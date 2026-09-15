"""项目 CRUD、对话历史、生成消息（SSE 流式）、预览托管。"""

import asyncio
import hashlib
import json
import math
import shutil
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..agent.loop import run_generation
from ..agent.prompts import (
    BREAKER_SYSTEM_PROMPT,
    CLARIFIER_SYSTEM_PROMPT,
    SPEC_AGENT_SYSTEM_PROMPT,
    build_system_prompt,
)
from ..agent.tools import (
    FileSandbox,
    SandboxViolation,
    build_breaker_tools,
    build_clarify_tools,
    build_tools,
    build_turn_result_tool,
    execute_tool,
    parse_clarify_payload,
    parse_ticket_payload,
    parse_turn_result_payload,
    recover_clarify_payload,
    recover_turn_result_payload,
    resolve_sandboxed,
)
from ..agent.verdicts import (
    CONSISTENT,
    FALLBACK,
    FILE_SET_MISMATCH,
    MISMATCH,
    UNDECLARED_CHANGE,
    VERBAL_COMPLETION,
)
from ..deps import COOKIE_NAME, get_current_user, get_db, resolve_user_by_token
from ..models import Message, Project, ProjectFile, Publication, Snapshot, Ticket, User, _utcnow
from ..public_links import remove_link
from ..rag.store import maybe_knowledge_store
from ..rate_limit import RateLimitRejected
from ..schemas import (
    ConfirmConsensusRequest,
    ConfirmPrdRequest,
    ConfirmSpecRequest,
    ConfirmTicketsRequest,
    CreateProjectRequest,
    FileContentOut,
    FileOut,
    MessageOut,
    ProjectOut,
    SendMessageRequest,
    SnapshotDetailOut,
    SnapshotDiffOut,
    SnapshotOut,
    TicketOut,
)
from ..snapshots import (
    create_snapshot,
    diff_snapshot,
    iter_project_files,
    list_snapshot_files,
    restore_snapshot,
)
from ..serving import serve_project_file

router = APIRouter(prefix="/api/projects", tags=["projects"])

# 同一项目的生成串行化：两路并发会互相覆盖项目目录文件，并在快照 rev 的
# 读后写上竞态（工单 0007 评审项）；按项目排队等待即可。
_generation_locks: dict[int, asyncio.Lock] = {}


def _project_lock(project_id: int) -> asyncio.Lock:
    return _generation_locks.setdefault(project_id, asyncio.Lock())


def project_dir(request: Request, project_id: int) -> Path:
    return Path(request.app.state.settings.storage_root) / "projects" / str(project_id)


def get_owned_project(project_id: int, user: User, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


def project_payload(db: Session, project: Project) -> dict:
    """项目响应体：附带活跃发布的 slug（工单 0006）。"""
    payload = ProjectOut.model_validate(project).model_dump(mode="json")
    payload["published_slug"] = db.scalar(
        select(Publication.slug).where(Publication.project_id == project.id)
    )
    return payload


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: CreateProjectRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = Project(user_id=user.id, name=body.name, mode=body.mode)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project_payload(db, project)


@router.get("", response_model=list[ProjectOut])
def list_projects(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[dict]:
    return [
        project_payload(db, p)
        for p in db.scalars(
            select(Project).where(Project.user_id == user.id).order_by(Project.updated_at.desc())
        )
    ]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return project_payload(db, get_owned_project(project_id, user, db))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    project = get_owned_project(project_id, user, db)
    # 先取发布 slug：删项目连带删发布记录，知识库里的沉淀条目一并移除（工单 0009）
    pub_slug = db.scalar(select(Publication.slug).where(Publication.project_id == project_id))
    db.execute(delete(Message).where(Message.project_id == project_id))
    db.execute(delete(ProjectFile).where(ProjectFile.project_id == project_id))
    db.execute(delete(Publication).where(Publication.project_id == project_id))
    db.execute(delete(Snapshot).where(Snapshot.project_id == project_id))
    db.execute(delete(Ticket).where(Ticket.project_id == project_id))
    db.delete(project)
    db.commit()
    shutil.rmtree(project_dir(request, project_id), ignore_errors=True)
    if pub_slug:
        # 连带移除 nginx 直出链接（工单 0013），公开入口立即消失
        remove_link(request.app.state.settings.storage_root, pub_slug)
        store = maybe_knowledge_store(request.app)
        if store is not None:
            try:
                store.remove_published(pub_slug)
            except Exception:  # noqa: BLE001 — 知识库清理失败不阻断删除（画廊检索以 DB 为准兜底）
                pass


@router.get("/{project_id}/messages", response_model=list[MessageOut])
def list_messages(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Message]:
    get_owned_project(project_id, user, db)
    return list(
        db.scalars(
            select(Message).where(Message.project_id == project_id).order_by(Message.id)
        )
    )


# “取全量历史”哨兵值：大于任何消息 id，工单执行期回灌上下文用（工单 0018）
_ALL_MESSAGES = 1 << 62


def _llm_history(db: Session, project_id: int, before_message_id: int, window: int) -> list:
    """把持久化对话转成最近 window 轮问答的 langchain 消息（跳过工具事件行）。

    窗口截断只影响喂给模型的上下文；持久化与回看仍是完整历史（工单 0004）。
    团队模式的 PRD 与确认消息（工单 0010）也入上下文：
    PRD 以 AI 消息、确认（含追加意见）以用户消息呈现。
    """
    rows = db.scalars(
        select(Message)
        .where(Message.project_id == project_id, Message.id < before_message_id)
        .order_by(Message.id)
    )
    history = []
    for m in rows:
        if m.kind in ("event", "thinking", "ticket") or m.role == "system":
            # 工具事件行、思考过程行、工单进度行与引导性系统消息不入上下文（工单 0010/0018）
            continue
        if m.role == "user":
            # 含 prd_confirm/consensus_confirm/spec_confirm/tickets_confirm：
            # 确认消息（可含追加意见）以用户消息呈现，后续阶段可见（工单 0010/0015/0016/0017）
            history.append(HumanMessage(content=m.content))
        elif m.kind == "prd":
            history.append(AIMessage(content=f"以下是我起草的 PRD：\n\n{m.content}"))
        elif m.kind == "consensus":
            # 需求共识入上下文：后续澄清轮次与工程师生成都以它为定案基础（工单 0015）
            history.append(AIMessage(content=f"以下是澄清后达成的需求共识：\n\n{m.content}"))
        elif m.kind == "spec":
            # 需求规格入上下文：重新起草与后续实现都以最新规格为准（工单 0016）
            history.append(AIMessage(content=f"以下是澄清后起草的需求规格：\n\n{m.content}"))
        elif m.kind == "tickets":
            # 工单清单入上下文：重新拆解在旧清单基础上调整，执行阶段可见清单（工单 0017）
            history.append(AIMessage(content=f"以下是拆解出的工单清单（JSON）：\n\n{m.content}"))
        elif m.kind == "clarify":
            # 选项式澄清问题入上下文：后续轮次据此不重复问已答内容（诊断修复）
            history.append(AIMessage(content=f"以下是我提出的澄清问题（含候选项，JSON）：\n\n{m.content}"))
        elif m.kind == "turn_result":
            # 轮次产物卡片入上下文（工单 0024）：后续轮次可见上一轮的结论摘要；
            # 原始 JSON 不入上下文——改动文件以迭代日志/系统提示的磁盘记录为权威，
            # 申报清单（declared_files）不回喂，避免模型把自己的申报当成事实复述。
            try:
                summary = str(json.loads(m.content).get("summary") or "").strip()
            except ValueError:
                summary = ""
            if summary:
                history.append(AIMessage(content=f"以下是我上一轮的轮次产物结论：{summary}"))
        else:
            history.append(AIMessage(content=m.content))
    if window > 0:
        history = history[-2 * window :]
    return history


def _prd_state(db: Session, project_id: int) -> str:
    """团队模式的 PRD 状态（工单 0010）：none | pending | confirmed。

    不新增表字段，从对话历史推导：最近一条 prd 消息之后是否存在确认消息。
    """
    last_prd_id = db.scalar(
        select(Message.id)
        .where(Message.project_id == project_id, Message.kind == "prd")
        .order_by(Message.id.desc())
        .limit(1)
    )
    if last_prd_id is None:
        return "none"
    confirmed = db.scalar(
        select(Message.id)
        .where(
            Message.project_id == project_id,
            Message.kind == "prd_confirm",
            Message.id > last_prd_id,
        )
        .limit(1)
    )
    return "confirmed" if confirmed is not None else "pending"


def _consensus_state(db: Session, project_id: int) -> str:
    """工程师模式的需求共识状态（工单 0015）：none | pending | confirmed。

    与 _prd_state 同构，从对话历史推导：最近一条 consensus 消息之后是否存在确认消息。
    """
    last_consensus_id = db.scalar(
        select(Message.id)
        .where(Message.project_id == project_id, Message.kind == "consensus")
        .order_by(Message.id.desc())
        .limit(1)
    )
    if last_consensus_id is None:
        return "none"
    confirmed = db.scalar(
        select(Message.id)
        .where(
            Message.project_id == project_id,
            Message.kind == "consensus_confirm",
            Message.id > last_consensus_id,
        )
        .limit(1)
    )
    return "confirmed" if confirmed is not None else "pending"


def _spec_state(db: Session, project_id: int) -> str:
    """团队模式的需求规格状态（工单 0016）：none | pending | confirmed。

    与 _consensus_state 同构，从对话历史推导：最近一条 spec 消息之后是否存在确认消息。
    """
    last_spec_id = db.scalar(
        select(Message.id)
        .where(Message.project_id == project_id, Message.kind == "spec")
        .order_by(Message.id.desc())
        .limit(1)
    )
    if last_spec_id is None:
        return "none"
    confirmed = db.scalar(
        select(Message.id)
        .where(
            Message.project_id == project_id,
            Message.kind == "spec_confirm",
            Message.id > last_spec_id,
        )
        .limit(1)
    )
    return "confirmed" if confirmed is not None else "pending"


def _tickets_state(db: Session, project_id: int) -> str:
    """团队模式的工单清单状态（工单 0017）：none | pending | confirmed。

    与 _spec_state 同构，从对话历史推导：最近一条 tickets 消息之后是否存在确认消息。
    """
    last_tickets_id = db.scalar(
        select(Message.id)
        .where(Message.project_id == project_id, Message.kind == "tickets")
        .order_by(Message.id.desc())
        .limit(1)
    )
    if last_tickets_id is None:
        return "none"
    confirmed = db.scalar(
        select(Message.id)
        .where(
            Message.project_id == project_id,
            Message.kind == "tickets_confirm",
            Message.id > last_tickets_id,
        )
        .limit(1)
    )
    return "confirmed" if confirmed is not None else "pending"


def _exec_state(db: Session, project_id: int) -> str:
    """团队模式的工单执行状态（工单 0018）：none | active | done。

    清单未确认是 none；已确认且仍有未完成（含失败/中断的 running）工单是 active；
    全部完成是 done，项目转入常规迭代（消息按次计数）。
    """
    if _tickets_state(db, project_id) != "confirmed":
        return "none"
    unfinished = db.scalar(
        select(Ticket.id)
        .where(Ticket.project_id == project_id, Ticket.status != "done")
        .limit(1)
    )
    return "active" if unfinished is not None else "done"


def _has_any_message(db: Session, project_id: int) -> bool:
    """项目是否已有对话消息：推导首建流水线是否已进入（名额语义，工单 0015 / ADR 0003）。

    首条消息即进入流水线的起点（名额在此扣一次）；此后只要还没有文件，
    无论澄清续轮还是共识确认触发的生成都不再计数，直到项目有文件后恢复按次计数。
    """
    return (
        db.scalar(select(Message.id).where(Message.project_id == project_id).limit(1))
        is not None
    )


def _sync_file_index(db: Session, project_id: int, root: Path) -> None:
    """以磁盘为准刷新文件索引表（不含快照等系统保留目录）。"""
    on_disk = {f.relative_to(root).as_posix(): f.stat().st_size for f in iter_project_files(root)}
    existing = {
        row.path: row
        for row in db.scalars(select(ProjectFile).where(ProjectFile.project_id == project_id))
    }
    for path, size in on_disk.items():
        if path in existing:
            existing[path].size = size
        else:
            db.add(ProjectFile(project_id=project_id, path=path, size=size))
    for path, row in existing.items():
        if path not in on_disk:
            db.delete(row)


def _existing_file_paths(db: Session, project_id: int) -> list[str]:
    """项目当前文件索引的路径清单（按路径排序），供分流判断用。"""
    return list(
        db.scalars(
            select(ProjectFile.path)
            .where(ProjectFile.project_id == project_id)
            .order_by(ProjectFile.path)
        )
    )


def _file_summaries(project_root: Path) -> list[dict]:
    """计算项目目录下每个文件的摘要：路径、行数、内容哈希前 8 位。

    供系统提示注入，让模型知道文件现状（哈希变了 = 记忆已过时，必须 read_file）。
    """
    summaries: list[dict] = []
    if not project_root.is_dir():
        return summaries
    for f in sorted(project_root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(project_root)
        if "snapshots" in rel.parts:
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        summaries.append({"path": rel.as_posix(), "lines": lines, "hash": sha})
    return summaries


def _fingerprint(summaries: list[dict]) -> dict[str, tuple[int, str]]:
    """指纹表：路径 → (行数, 内容哈希)。轮前/轮末两份比对即得真实改动集（工单 0022）。"""
    return {s["path"]: (s["lines"], s["hash"]) for s in summaries}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# 执行流里被 on_event 压下的事件（如每单的 done/单内 error）：序列化结果固定，循环内直接比对过滤（工单 0018）
_NOOP_CHUNK = _sse({"type": "noop"})


def _sse_response(stream) -> StreamingResponse:
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- 生成限流（工单 0011） ---
#
# 模型调用（工程师生成等）在入口处接受限流检查：
# 超限直接 429（携带建议重试时间与 Retry-After 头），不落用户消息、不调模型；
# 引导类响应（如未确认 PRD 的提示）不调模型，不计入限额；
# 首建流水线内的后续阶段（澄清续轮、共识/规格确认、拆单、工单清单确认）不另计数（ADR 0003）。
# 全局名额自接受起占用，流结束（成功/失败/断流）时释放。


def _format_wait(seconds: float) -> str:
    """把建议等待秒数转成友好表述（用于限流提示文案）。"""
    s = max(1, math.ceil(seconds))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{math.ceil(s / 60)} 分钟"
    return f"{math.ceil(s / 3600)} 小时"


def _accept_rate_limit(request: Request, user: User) -> None:
    """接受一次生成；超限抛 429，响应体携带 reason/retry_after 与友好文案。"""
    limiter = request.app.state.rate_limiter
    try:
        limiter.accept(user.id)
    except RateLimitRejected as e:
        retry_after = int(math.ceil(e.retry_after))
        if e.reason == "user_hourly":
            message = (
                f"已达每小时生成上限（{limiter.per_user_hourly} 次），"
                f"请约 {_format_wait(e.retry_after)}后重试"
            )
        else:
            message = f"当前同时进行生成任务较多，请约 {_format_wait(e.retry_after)}后重试"
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "reason": e.reason,
                "retry_after": retry_after,
                "message": message,
            },
            headers={"Retry-After": str(retry_after)},
        )


async def _limited_stream(stream, request: Request):
    """包一层释放：无论流如何结束都归还全局并发名额（工单 0011）。"""
    try:
        async for chunk in stream:
            yield chunk
    finally:
        request.app.state.rate_limiter.release()


class _SubmitTurnResultInvoked(Exception):
    """工程师调用 submit_turn_result 的控制流信号：携轮次产物终止本轮循环（工单 0024）。"""

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__("submit_turn_result")


# --- 自洽性核验（工单 0025 / ADR 0005「第 8 层」） ---
#
# 取代已退役的中文措辞检测（固定短语表 + 正则族 + 三级升级链）：不猜模型说了什么，
# 只比对模型申报的改动文件集与磁盘真实改动集（工单 0022 指纹 diff）。纯集合运算，
# 判定零成本、确定性，对任何语言的任何措辞免疫。
# 核验结论与失配种类的词表在 agent/verdicts.py（工单 0026：与迭代日志的模型侧
# 渲染文案共用，消除字面量双写漂移）。


def _norm_declared_paths(intent: str, paths: list[str]) -> set[str]:
    """申报路径归一化（仅用于比对，不改写申报原样）：反斜杠转正斜杠、去 ./ 与前导 / 前缀。

    intent=no_change 即声明零改动——即便 changed_files 误列了文件也以意图为准
    （自相矛盾的申报不能靠「文件恰好对上」洗白成自洽）。
    """
    if intent == "no_change":
        return set()
    normalized: set[str] = set()
    for p in paths:
        n = p.replace("\\", "/")
        while n.startswith("./"):
            n = n[2:]
        n = n.lstrip("/")
        if n:
            normalized.add(n)
    return normalized


def _consistency_verdict(
    intent: str, declared: set[str], touched: set[str]
) -> tuple[str, str | None]:
    """自洽性判定：返回 (结论, 失配类型)。

    三种失配（工单 0025 验收）：
    - verbal_completion：声明有改动（申报集非空，或自报 modify_code）而磁盘零改动；
    - undeclared_change：声明零改动而磁盘有改动；
    - file_set_mismatch：申报文件集与磁盘真实文件集不符（两侧均非空）。
    """
    if not touched and (declared or intent == "modify_code"):
        return MISMATCH, VERBAL_COMPLETION
    if not declared and touched:
        return MISMATCH, UNDECLARED_CHANGE
    if declared != touched:
        return MISMATCH, FILE_SET_MISMATCH
    return CONSISTENT, None


def _consistency_feedback(kind: str, declared: set[str], touched: set[str]) -> str:
    """失配回喂文案：把「你声明的 vs 磁盘实际的」精确差异摆给模型（每轮至多一次）。"""
    declared_desc = ", ".join(sorted(declared)) if declared else "（无）"
    touched_desc = (
        ", ".join(sorted(touched)) if touched else "（无——本轮磁盘上没有产生任何文件改动）"
    )
    return (
        f"自洽性核验失配（{kind}）：你的申报与磁盘真实改动不符。\n"
        f"你声明改动的文件：{declared_desc}\n"
        f"磁盘实际改动的文件：{touched_desc}\n"
        "若改动尚未实际完成，先用文件工具完成真实改动；"
        "若申报有误，重新调用 submit_turn_result，如实填写 intent 与 changed_files。"
    )


async def _engineer_stream(
    request: Request,
    project_id: int,
    user_text: str,
    history: list,
    result: dict | None = None,
    on_event: Callable[[dict], dict] | None = None,
    extra_finalize: Callable[[Session, Snapshot], None] | None = None,
    record_iteration: bool = True,
):
    """工程师智能体生成流（SSE 块）：工程师模式与团队模式确认后共用（工单 0010）。

    串行执行（工单 0018）通过三个可选挂接复用本流：
    - result：记录收尾结果 {"ok": bool}，成功时附 snapshot（检查点），失败时附 error；
    - on_event：外发前改写事件（如把每单的 done 换成工单进度事件）；
    - extra_finalize：收尾落盘同一事务内的附加写入（如工单标 done 与检查点引用）。
    - record_iteration：是否将本轮计入迭代日志（Layer 5）。团队工单执行是内部编排而非
      用户对话轮，传 False——既不读也不写迭代日志，避免把工单指令当“用户说”注入后续提示。
    """
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory

    def _emit(data: dict) -> str:
        if on_event is not None:
            data = on_event(data)
        return _sse(data)

    try:
        model = request.app.state.model_factory(settings)
    except Exception as e:  # noqa: BLE001 — 未配置 Key 等环境问题以 error 事件收尾
        if result is not None:
            result["ok"] = False
        yield _emit({"type": "error", "detail": str(e)})
        return

    sandbox = FileSandbox(project_dir(request, project_id))
    # 知识库可用时附带 search_templates 检索工具（工单 0009）；不可用时降级为纯文件工具
    tools = build_tools(sandbox, maybe_knowledge_store(request.app))
    # 唯一终结出口（工单 0024 / ADR 0005「第 8 层」）：与文件工具同一清单绑定，
    # 首建轮、迭代轮与团队工单执行共用本路径，不单加开关。
    # 出口以工具调用为载体而非 response_format——二者是互斥的输出通道。
    tools = [*tools, *build_turn_result_tool()]

    def engineer_executor(tool_list, name, args):
        """拦截终结出口（工单 0024/0025）：非法产物错误文案交还模型修正（同澄清/拆单
        范式）；合法产物先过自洽性核验——申报与磁盘失配时把精确差异作为工具结果回喂
        一次，修正后（或第二次提交仍失配时）抛控制流信号终止循环。"""
        if name == "submit_turn_result":
            raw = str((args or {}).get("payload", ""))
            parsed, reason = parse_turn_result_payload(raw)
            if parsed is None:
                return False, f"轮次产物不合法：{reason} 请修正后重新调用 submit_turn_result。"
            feedback = _check_consistency(parsed)
            if feedback is not None:
                return False, feedback
            raise _SubmitTurnResultInvoked(parsed)
        return execute_tool(tool_list, name, args)

    # 迭代日志（Layer 5）：读入历轮改动摘要注入系统提示，弥补对话窗口截断的失忆。
    # 团队工单执行（record_iteration=False）是内部编排而非用户对话轮，不读迭代日志。
    iteration_log: list = []
    if record_iteration:
        with session_factory() as session:
            _row = session.get(Project, project_id)
            iteration_log = list(_row.iteration_log) if _row and _row.iteration_log else []
    # 轮前指纹（工单 0022 / ADR 0005「权威来源归位」）：改动集的权威来源是磁盘状态。
    # _file_summaries 本就读磁盘（路径 + 行数 + 内容哈希），系统提示注入复用同一次扫描；
    # 基准取磁盘而非上一个快照——越界轮不建快照会使快照序列与磁盘序列脱钩。
    summaries = _file_summaries(sandbox.root)
    system_prompt = build_system_prompt(summaries, iteration_log)
    pre_round = _fingerprint(summaries)

    # 自洽性核验状态（工单 0025）：每轮至多一次精确差异回喂，出口工具路径与
    # 正文恢复路径共用同一计数——回喂是轮级预算，不是按提交次数重置的。
    consistency = {"refeed_done": False}

    def _touched_now() -> set[str]:
        """以当前磁盘重算指纹，与轮前指纹比对即得本轮至此的真实改动集。"""
        post = _fingerprint(_file_summaries(sandbox.root))
        return {p for p in set(pre_round) | set(post) if pre_round.get(p) != post.get(p)}

    def _check_consistency(parsed: dict) -> str | None:
        """核验轮次产物申报：失配且尚未回喂过 → 返回精确差异回喂文案；否则 None 放行。"""
        declared = _norm_declared_paths(parsed["intent"], parsed["changed_files"])
        touched_now = _touched_now()
        verdict, kind = _consistency_verdict(parsed["intent"], declared, touched_now)
        if verdict == CONSISTENT or consistency["refeed_done"]:
            return None
        consistency["refeed_done"] = True
        return _consistency_feedback(kind or "", declared, touched_now)

    # ADR 0005 降级链（工单 0025 完整落地）：模型试图以普通文本收尾时——
    # ① 按房规从累积正文恢复产物 JSON（成功即经自洽性核验后以终结信号收束）；
    # ② 恢复不了则回喂一次唯一出口提示；
    # ③ 仍不听 → 放行 done，路由层以首段散文作 summary、按磁盘事实兜底成卡片。
    exit_state: dict = {"prose_refeed_done": False, "first_prose": None}

    def final_text_hook(final_text: str) -> str | None:
        recovered = recover_turn_result_payload("".join(raw_parts))
        if recovered is not None:
            feedback = _check_consistency(recovered)
            if feedback is not None:
                return feedback
            raise _SubmitTurnResultInvoked(recovered)
        if not exit_state["prose_refeed_done"]:
            exit_state["prose_refeed_done"] = True
            exit_state["first_prose"] = final_text
            return (
                "你正在用普通文本收尾。本轮唯一合法的收尾方式是调用 submit_turn_result "
                "工具提交轮次产物（intent / summary / changed_files / no_change_reason），"
                "请立即调用。"
            )
        return None

    done_data: dict | None = None
    turn_result_payload: dict | None = None
    thinking_parts: list[str] = []
    raw_parts: list[str] = []
    try:
        async for event in run_generation(
            model,
            tools,
            engineer_executor,
            system_prompt,
            history,
            user_text,
            max_steps=settings.agent_max_steps,
            max_retries=settings.agent_max_retries,
            final_text_hook=final_text_hook,
        ):
            if event.type == "done":
                # done 先扣下：落盘完成后才外发，保证它是流的最后一个事件
                done_data = event.data
            else:
                if event.type == "thinking":
                    # 思考增量另存一份：正常收尾合并落库；中断时也据已流出部分落库（诊断修复）
                    thinking_parts.append(event.data.get("content", ""))
                if event.type in ("thinking", "text"):
                    # 原始输出留一份：模型把产物 JSON 写进正文时据此恢复（房规同澄清流）
                    raw_parts.append(event.data.get("content", ""))
                if event.type == "tool" and event.data.get("name") == "submit_turn_result":
                    # 终结出口的工具事件不外发：对用户本轮只呈现产物卡片（事件行的
                    # 展示与折叠归工单 0023）。落库仍按 _persist_event 既有契约
                    # （ADR 0005：刷新页面可回看完整过程，是排查越界改动的证据链）——
                    # 非法 payload 的修正尝试照样留档；合法出口时循环在发出 done 事件
                    # 前即抛控制流信号，其完整 payload 由卡片消息本身留档。
                    if event.data.get("status") != "start":
                        _persist_event(session_factory, project_id, event.data)
                    continue
                if result is not None and event.type == "error":
                    result["ok"] = False
                    result["error"] = event.data.get("detail", "")
                yield _emit({"type": event.type, **event.data})
                if event.type == "tool":
                    # 事件行只是「给人看的过程记录」（工单 0022）：不再从 args.path
                    # 解析改动集（summarize_args 会截断超长参数，那是运气不是设计），
                    # 三处硬闸的判据一律来自轮末磁盘指纹比对。
                    if event.data.get("status") != "start":
                        _persist_event(session_factory, project_id, event.data)
    except _SubmitTurnResultInvoked as invoked:
        # 终结信号：本轮到此为止，轮次产物进入结构化收尾（散文路径原样兜底）
        turn_result_payload = invoked.payload
    except Exception as e:  # noqa: BLE001 — 流式过程中的意外以 error 事件收尾，思考已流出部分仍落库（诊断修复）
        if result is not None:
            result["ok"] = False
            result["error"] = f"生成中断: {e}"
        _persist_partial_thinking(session_factory, project_id, "engineer", thinking_parts)
        yield _emit({"type": "error", "detail": f"生成中断: {e}"})
        return
    except BaseException:
        # 刷新/断流触发的生成器关闭（GeneratorExit/CancelledError）：
        # 不能在此 yield（已关闭），只把已流出的思考落库后照旧退出（诊断修复）
        _persist_partial_thinking(session_factory, project_id, "engineer", thinking_parts)
        raise

    # 轮末重算指纹，与轮前指纹比对即得本轮真实改动的文件集（工单 0022）：
    # 新增 / 内容变化 / 删除（工具虽无删除能力，外部改动同样被磁盘比对捕获）全部覆盖；
    # 沙箱侧 modified_this_round 是同构辅助记录，各硬闸的判据只看这里的磁盘比对结果。
    touched_files = _touched_now()

    # 降级链末端（ADR 0005 / 工单 0025）：回喂一次后模型仍以普通文本收尾 →
    # 系统按磁盘事实兜底成卡片：首段散文作 summary，意图按磁盘真实改动判定，
    # 声明改动一律以磁盘真实值填充、绝不采信模型申报。兜底不没收用户的工作成果
    # （降级铁律）：磁盘有改动照样建快照、入迭代日志。
    fallback_used = False
    if turn_result_payload is None and done_data is not None:
        prose = exit_state["first_prose"] or done_data.get("text", "")
        fallback_used = True
        turn_result_payload = {
            "intent": "modify_code" if touched_files else "no_change",
            "summary": prose.strip() or "（本轮由系统兜底收尾，模型未提交总结。）",
            "changed_files": [],
            "no_change_reason": (
                ""
                if touched_files
                else "模型未经 submit_turn_result 提交轮次产物，系统按磁盘事实收尾：本轮无文件改动。"
            ),
        }
    completed = done_data is not None or turn_result_payload is not None
    no_change_round = bool(record_iteration and completed and not touched_files)

    # 自洽性核验结论（工单 0025）：以轮末磁盘比对为权威，结构化字段呈现
    # （卡片徽标 + 收尾事件），不再往 summary 或回复正文追加说明文案。
    # 兜底轮没有申报可比，结论恒为 fallback。
    if fallback_used:
        verdict, mismatch_kind = FALLBACK, None
    elif turn_result_payload is not None:
        verdict, mismatch_kind = _consistency_verdict(
            turn_result_payload["intent"],
            _norm_declared_paths(
                turn_result_payload["intent"], turn_result_payload["changed_files"]
            ),
            touched_files,
        )
    else:
        verdict, mismatch_kind = None, None

    snapshot: Snapshot | None = None
    card_json: str | None = None
    final_summary = ""
    try:
        with session_factory() as session:
            thinking_text = "".join(thinking_parts).strip()
            if thinking_text:
                session.add(
                    Message(project_id=project_id, role="engineer", kind="thinking", content=thinking_text)
                )
            # 快照先行：产物卡片要携快照引用（展开看本轮 diff 的入口）。
            # 每次成功生成（首轮与迭代）自动留档一版快照；失败的生成不留档（工单 0007）。
            # 零改动的用户对话轮不留档（硬闸）：磁盘与上一版完全一致，建快照只会
            # 制造“版本 N+1”的假进展、污染回滚列表；轮次结局以磁盘事实为准。
            # 团队工单执行（record_iteration=False）不受此影响，检查点语义保持原样。
            if completed and not no_change_round:
                snapshot = create_snapshot(
                    session, project_id, sandbox.root, settings.snapshot_max_kept
                )
                session.flush()  # 卡片落库前需要 snapshot.id
                if result is not None:
                    result["snapshot"] = snapshot
                if extra_finalize is not None:
                    extra_finalize(session, snapshot)
            if turn_result_payload is not None:
                # 轮次产物卡片（工单 0024）：改动文件清单一律以磁盘真实改动为权威
                # （0022 成果），绝不采信模型申报；申报清单原样留在 declared_files，
                # 与 consistency/mismatch_kind 核验结论字段成对呈现（工单 0025），
                # 只到文件级、不涉区域。summary 保持模型原文：核验结论以卡片徽标
                # 呈现，不再往正文追加系统核验说明文案。
                final_summary = turn_result_payload["summary"]
                card = {
                    "intent": turn_result_payload["intent"],
                    "summary": final_summary,
                    "changed_files": sorted(touched_files),
                    "declared_files": turn_result_payload["changed_files"],
                    "no_change_reason": turn_result_payload["no_change_reason"],
                    "consistency": verdict,
                    "mismatch_kind": mismatch_kind,
                    "snapshot_id": snapshot.id if snapshot is not None else None,
                    "snapshot_rev": snapshot.rev if snapshot is not None else None,
                }
                card_json = json.dumps(card, ensure_ascii=False)
                session.add(
                    Message(
                        project_id=project_id,
                        role="engineer",
                        kind="turn_result",
                        content=card_json,
                    )
                )
            _sync_file_index(session, project_id, sandbox.root)
            project_row = session.get(Project, project_id)
            if project_row is not None:
                project_row.updated_at = _utcnow()
                # Layer 5（工单 0026 / ADR 0005「迭代日志调整」）：仅成功的对话轮留痕，
                # JSON 列整体重赋值以触发 SQLAlchemy 变更检测。条目按「改动」累积——
                # seq 语义是「第 N 次改动」（咨询轮不入账后编号会与对话轮错位，继续叫
                # 「第 N 轮」等于对模型说谎）；user_text 完整落库用户原话（蒸馏版是模型
                # 写的、可能失真，原话才是权威数据；截断/蒸馏只发生在渲染注入侧）；
                # verdict/mismatch_kind 把核验结论入账——失配（及 0028 引入的越界、
                # 未完成）均须入账，日志追加不以快照留档为门槛：越界轮虽不留档快照，
                # 改动却留在磁盘上，不留这条日志，后续轮次无从理解磁盘为什么是现在
                # 这个样子。暂不设截断或聚合上限（净 token 账持平或更短，不预先优化）。
                if completed and record_iteration:
                    log = list(project_row.iteration_log or [])
                    log.append(
                        {
                            "seq": len(log) + 1,
                            "user_text": user_text,
                            "files": sorted(touched_files),
                            "verdict": verdict,
                            "mismatch_kind": mismatch_kind,
                        }
                    )
                    project_row.iteration_log = log
            session.commit()
    except Exception as e:  # noqa: BLE001 — 收尾落盘失败也须以 error 事件告知，不得静默断流
        if result is not None:
            result["ok"] = False
            result["error"] = f"生成收尾失败: {e}"
        yield _emit({"type": "error", "detail": f"生成收尾失败: {e}"})
        return
    if completed:
        if result is not None:
            result["ok"] = True
        # 结构化收尾（工单 0024/0025）：卡片事件携完整 payload 外发；收尾事件
        # 移除随措辞机器退役的 warning/no_change 布尔与文案字段，改为携带核验
        # 结论（verdict，失配时附 mismatch_kind）与产物摘要（artifact）；
        # 落盘完成后才外发，done 仍是流的最后一个事件。
        yield _emit({"type": "turn_result", "content": card_json})
        done_payload = {
            "type": "done",
            "text": final_summary,
            "verdict": verdict,
            "artifact": {
                "intent": turn_result_payload["intent"],
                "summary": final_summary,
                "changed_files": sorted(touched_files),
            },
        }
        if mismatch_kind is not None:
            done_payload["mismatch_kind"] = mismatch_kind
        yield _emit(done_payload)


async def _spec_stream(request: Request, project_id: int, user_text: str, history: list):
    """需求规格智能体产规格流（工单 0016）：无工具，文本以 spec 事件流式外发。

    与旧 PM 产 PRD 流同构（工单 0010 退役后的承接者）：成功后规格以
    role=spec_agent, kind=spec 落对话历史；不写文件、不留快照。
    """
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    try:
        model = request.app.state.model_factory(settings)
    except Exception as e:  # noqa: BLE001 — 与其他阶段一致，环境问题以 error 事件收尾
        yield _sse({"type": "error", "detail": str(e)})
        return

    done_data: dict | None = None
    errored = False
    thinking_parts: list[str] = []
    try:
        async for event in run_generation(
            model,
            [],
            execute_tool,
            SPEC_AGENT_SYSTEM_PROMPT,
            history,
            user_text,
            max_steps=settings.agent_max_steps,
            max_retries=settings.agent_max_retries,
        ):
            if event.type == "done":
                done_data = event.data
            elif event.type == "text":
                yield _sse({"type": "spec", "content": event.data.get("content", "")})
            else:
                if event.type == "error":
                    errored = True
                if event.type == "thinking":
                    thinking_parts.append(event.data.get("content", ""))
                yield _sse({"type": event.type, **event.data})
    except Exception as e:  # noqa: BLE001 — 流式过程中的意外以 error 事件收尾
        _persist_partial_thinking(session_factory, project_id, "spec_agent", thinking_parts)
        yield _sse({"type": "error", "detail": f"生成中断: {e}"})
        return
    except BaseException:
        # 刷新/断流触发的生成器关闭：落库已流出的思考后照旧退出（同 PM 流）
        _persist_partial_thinking(session_factory, project_id, "spec_agent", thinking_parts)
        raise

    spec_text = done_data.get("text", "") if done_data else ""
    if not spec_text:
        # 循环已以 error 事件收尾时不再重复报错（如模型调用失败/超步数）
        if not errored:
            yield _sse({"type": "error", "detail": "规格智能体未产出需求规格"})
        return
    try:
        with session_factory() as session:
            thinking_text = "".join(thinking_parts).strip()
            if thinking_text:
                session.add(
                    Message(
                        project_id=project_id, role="spec_agent", kind="thinking", content=thinking_text
                    )
                )
            session.add(
                Message(project_id=project_id, role="spec_agent", kind="spec", content=spec_text)
            )
            project_row = session.get(Project, project_id)
            if project_row is not None:
                project_row.updated_at = _utcnow()
            session.commit()
    except Exception as e:  # noqa: BLE001 — 规格落库失败须明示，不得静默断流
        yield _sse({"type": "error", "detail": f"需求规格保存失败: {e}"})
        return
    yield _sse({"type": "done", "text": spec_text})


class _SubmitTicketsInvoked(Exception):
    """拆单智能体调用 submit_tickets 的控制流信号：携工单清单 JSON 终止循环。"""

    def __init__(self, raw: str):
        self.raw = raw
        super().__init__("submit_tickets")


async def _break_stream(request: Request, project_id: int, user_text: str, history: list):
    """拆单流（工单 0017）：规格确认后拆解为纵向切片工单清单卡片。

    拆单智能体唯一工具是 submit_tickets（无任何文件工具，同澄清范式）：
    - 清单非法（解析/校验失败）→ 错误文案经 ToolMessage 回传模型自行修正；
    - 清单合法 → 以 tickets 事件外发并落库；旧的待确认清单整批替换，
      新清单的序号在历史最大值上续编。不写文件、不留快照。
    """
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    try:
        model = request.app.state.model_factory(settings)
    except Exception as e:  # noqa: BLE001 — 与其他阶段一致，环境问题以 error 事件收尾
        yield _sse({"type": "error", "detail": str(e)})
        return

    def breaker_executor(tools, name, args):
        if name == "submit_tickets":
            raw = str((args or {}).get("tickets", ""))
            tickets, reason = parse_ticket_payload(raw)
            if tickets is None:
                # 非法清单交还模型修正，不中断循环（重试上限兜底）
                return False, f"工单清单不合法：{reason} 请修正后重新调用 submit_tickets。"
            raise _SubmitTicketsInvoked(raw)
        return execute_tool(tools, name, args)

    submitted_raw: str | None = None
    done_data: dict | None = None
    errored = False
    thinking_parts: list[str] = []
    try:
        async for event in run_generation(
            model,
            build_breaker_tools(),
            breaker_executor,
            BREAKER_SYSTEM_PROMPT,
            history,
            user_text,
            max_steps=settings.agent_max_steps,
            max_retries=settings.agent_max_retries,
        ):
            if event.type == "done":
                done_data = event.data
            else:
                if event.type == "error":
                    errored = True
                if event.type == "thinking":
                    thinking_parts.append(event.data.get("content", ""))
                # submit_tickets 的工具事件不外发：拆单对用户只呈现清单卡片（同澄清）
                if event.type != "tool":
                    yield _sse({"type": event.type, **event.data})
    except _SubmitTicketsInvoked as invoked:
        submitted_raw = invoked.raw
    except Exception as e:  # noqa: BLE001 — 流式过程中的意外以 error 事件收尾
        _persist_partial_thinking(session_factory, project_id, "breaker_agent", thinking_parts)
        yield _sse({"type": "error", "detail": f"拆单中断: {e}"})
        return
    except BaseException:
        # 刷新/断流触发的生成器关闭：落库已流出的思考后照旧退出（同澄清流）
        _persist_partial_thinking(session_factory, project_id, "breaker_agent", thinking_parts)
        raise

    if submitted_raw is None:
        # 模型未调 submit_tickets：拆解未收敛，明示错误（提示词已要求必调工具）
        if not errored:
            yield _sse({"type": "error", "detail": "拆单未产出工单清单"})
        return

    # 产出工单清单：卡片内容外发并落库，等待用户确认后才进入执行期（确认门，ADR 0003）
    yield _sse({"type": "tickets", "content": submitted_raw})
    try:
        with session_factory() as session:
            thinking_text = "".join(thinking_parts).strip()
            if thinking_text:
                session.add(
                    Message(
                        project_id=project_id,
                        role="breaker_agent",
                        kind="thinking",
                        content=thinking_text,
                    )
                )
            session.add(
                Message(
                    project_id=project_id,
                    role="breaker_agent",
                    kind="tickets",
                    content=submitted_raw,
                )
            )
            # 重新拆解取代旧清单：先取历史最大序号再删旧的待确认工单，新清单续编；
            # 已确认的清单不可被重拆（分流已拦截），无需在此设防。
            # blocked_by 提交时按清单内 1 起编号，落库时换算为续编后的 seq，保证引用不悬空（工单 0017）
            base_seq = session.scalar(
                select(Ticket.seq).where(Ticket.project_id == project_id).order_by(Ticket.seq.desc())
            ) or 0
            session.execute(
                delete(Ticket).where(Ticket.project_id == project_id, Ticket.status == "open")
            )
            parsed, _ = parse_ticket_payload(submitted_raw)
            for item in parsed or []:
                seq = base_seq + item["seq"]
                session.add(
                    Ticket(
                        project_id=project_id,
                        seq=seq,
                        title=item["title"],
                        deliverable=item["deliverable"],
                        blocked_by=json.dumps(
                            [base_seq + b for b in item["blocked_by"]], ensure_ascii=False
                        ),
                        status="open",
                    )
                )
            project_row = session.get(Project, project_id)
            if project_row is not None:
                project_row.updated_at = _utcnow()
            session.commit()
    except Exception as e:  # noqa: BLE001 — 工单落库失败须明示，不得静默断流
        yield _sse({"type": "error", "detail": f"工单清单保存失败: {e}"})
        return
    yield _sse({"type": "done", "text": submitted_raw})


# --- 工单检查点串行执行（工单 0018 / ADR 0003） ---
#
# 清单确认后（或失败重试/断线继续，见 /tickets/resume）：按序号升序逐个执行未完成工单；
# 拆单校验保证 blocked_by 只引用更小序号，序号升序即依赖序。一次只执行一个，
# 完成一个再进下一个；每单完成即形成检查点快照（融入现有快照体系），
# 失败则停在该单起点，重试/继续不从第一单重来。


def _next_pending_ticket(session: Session, project_id: int) -> Ticket | None:
    """第一张未完成工单（含失败与中断遗留的 running）；全部完成返回 None。

    执行器断在哪个环节就从哪里续：失败单原地重试、中断单（running）重新执行，
    已完成的单绝不重跑。
    """
    return session.scalar(
        select(Ticket)
        .where(Ticket.project_id == project_id, Ticket.status != "done")
        .order_by(Ticket.seq)
        .limit(1)
    )


def _ticket_prompt(current: dict, all_payloads: list[dict]) -> str:
    """单张工单的执行指令：附完整清单划定边界，但只交付当前这一单。

    确认/反馈消息已在全量历史里，指令不再重复，只给本次执行目标（工单 0018）。
    """
    lines = [
        f"{p['seq']}. {p['title']}：{p['deliverable']}"
        + ("（已完成）" if p["status"] == "done" else "")
        for p in all_payloads
    ]
    return (
        f"工单清单：\n" + "\n".join(lines) + "\n\n"
        f"现在执行工单 {current['seq']}「{current['title']}」，交付内容：{current['deliverable']}\n"
        f"只完成本工单的交付内容：已完成的工单不要重做，后面工单的内容不要提前实现，完成后停止。"
    )


def _persist_ticket_progress(session_factory, project_id: int, data: dict) -> None:
    """工单进度行落库（kind=ticket）：断线重连后刷新可回看执行进度（工单 0018）。"""
    with session_factory() as session:
        session.add(
            Message(
                project_id=project_id,
                role="engineer",
                kind="ticket",
                content=json.dumps(data, ensure_ascii=False),
            )
        )
        session.commit()


async def _exec_tickets_stream(request: Request, project_id: int):
    """工单串行执行流（工单 0018）：逐单执行，每单完成形成检查点快照。

    单张工单复用工程师流（_engineer_stream）：完成在工程师收尾同一事务内标 done
    并记录检查点快照引用（要么都成要么都不成）；其 done/单内 error 经 on_event 压下，
    由本流统一发工单进度事件；失败标 failed 后终止，由 /tickets/resume 从该单起点重试。
    """
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    while True:
        with session_factory() as session:
            payloads = [
                _ticket_payload(row)
                for row in session.scalars(
                    select(Ticket).where(Ticket.project_id == project_id).order_by(Ticket.seq)
                )
            ]
            total = len(payloads)
            ticket = _next_pending_ticket(session, project_id)
            if ticket is None:
                yield _sse(
                    {"type": "done", "text": f"全部 {total} 张工单执行完成，项目已就绪。"}
                )
                return
            done_count = sum(1 for p in payloads if p["status"] == "done")
            ticket_id, seq, title = ticket.id, ticket.seq, ticket.title
            current_payload = next(p for p in payloads if p["seq"] == seq)
            ticket.status = "running"
            session.commit()
            # 无“当前消息”可排除：以哨兵值取全量历史（窗口截断由 window 控制）
            history = _llm_history(
                session,
                project_id,
                before_message_id=_ALL_MESSAGES,
                window=settings.agent_history_window,
            )

        yield _sse(
            {
                "type": "ticket_progress",
                "seq": seq,
                "title": title,
                "status": "running",
                "done": done_count,
                "total": total,
            }
        )

        result: dict = {}

        def _mark_done(_session: Session, snapshot: Snapshot) -> None:
            # 与工程师收尾落盘同一事务：状态与检查点引用要么都成要么都不成（工单 0018）
            row = _session.get(Ticket, ticket_id)
            if row is not None:
                row.status = "done"
                row.snapshot_id = snapshot.id

        def _rewrite(event: dict) -> dict:
            # 每单的 done/error 压下：成功由本流发 done 进度，失败由本流补 failed 进度与 error，
            # 避免单内错误文案先于工单状态更新外发造成观感错序（工单 0018）
            if event["type"] in ("done", "error"):
                return {"type": "noop"}
            return event

        try:
            async for chunk in _engineer_stream(
                request,
                project_id,
                _ticket_prompt(current_payload, payloads),
                history,
                result=result,
                on_event=_rewrite,
                extra_finalize=_mark_done,
                record_iteration=False,
            ):
                if chunk != _NOOP_CHUNK:
                    yield chunk
        except BaseException:
            # 断流：工单留在 running 状态，重连后由 /tickets/resume 从该单重新执行（工单 0018）
            raise

        if not result.get("ok"):
            with session_factory() as session:
                row = session.get(Ticket, ticket_id)
                if row is not None:
                    row.status = "failed"
                    session.commit()
            detail = result.get("error") or "未知原因"
            progress = {
                "type": "ticket_progress",
                "seq": seq,
                "title": title,
                "status": "failed",
                "done": done_count,
                "total": total,
            }
            _persist_ticket_progress(session_factory, project_id, progress)
            yield _sse(progress)
            yield _sse(
                {"type": "error", "detail": f"工单 {seq}「{title}」执行失败：{detail} 可从该工单重试。"}
            )
            return

        snapshot = result.get("snapshot")
        progress = {
            "type": "ticket_progress",
            "seq": seq,
            "title": title,
            "status": "done",
            "done": done_count + 1,
            "total": total,
            "snapshot_rev": snapshot.rev if snapshot is not None else None,
        }
        _persist_ticket_progress(session_factory, project_id, progress)
        yield _sse(progress)
        # 进入下一单；每单重取历史与文件清单，前序交付成果自然进入上下文


class _AskOptionsInvoked(Exception):
    """澄清智能体调用 ask_options 的控制流信号：携选项式问题清单 JSON 终止循环。"""

    def __init__(self, raw: str):
        self.raw = raw
        super().__init__("ask_options")


class _StartBuildInvoked(Exception):
    """澄清智能体调用 start_build 的控制流信号：携需求共识摘要立即终止循环。"""

    def __init__(self, summary: str):
        self.summary = summary
        super().__init__("start_build")


async def _clarify_stream(request: Request, project_id: int, user_text: str, history: list):
    """需求澄清流（工单 0015）：分轮问答直至无未决问题，产出需求共识卡片。

    澄清智能体只绑定提问与收敛两个工具（无任何文件工具）：
    - 模型调用 ask_options → 选项式问题以 clarify 事件外发并落库，前端渲染可点选卡片；
      清单非法时错误文案经 ToolMessage 回传模型自行修正（同拆单范式）；
    - 模型返回纯文本 → 兼容旧形态的澄清提问，以 role=clarifier 落历史；
    - 模型调用 start_build → 需求共识以 consensus 事件流式外发并落库，本轮结束；
      两个工具的 ToolMessage 都不回传模型，避免模型在提问/共识后继续输出。
    """
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    try:
        model = request.app.state.model_factory(settings)
    except Exception as e:  # noqa: BLE001 — 与其他阶段一致，环境问题以 error 事件收尾
        yield _sse({"type": "error", "detail": str(e)})
        return

    def clarify_executor(tools, name, args):
        if name == "start_build":
            raise _StartBuildInvoked(str((args or {}).get("requirements_summary", "")))
        if name == "ask_options":
            raw = str((args or {}).get("questions", ""))
            questions, reason = parse_clarify_payload(raw)
            if questions is None:
                # 非法清单交还模型修正，不中断循环（重试上限兜底，同拆单）
                return False, f"问题清单不合法：{reason} 请修正后重新调用 ask_options。"
            raise _AskOptionsInvoked(json.dumps(questions, ensure_ascii=False))
        return execute_tool(tools, name, args)

    consensus_summary: str | None = None
    clarify_payload: str | None = None
    done_data: dict | None = None
    errored = False
    thinking_parts: list[str] = []
    raw_parts: list[str] = []
    try:
        async for event in run_generation(
            model,
            build_clarify_tools(),
            clarify_executor,
            CLARIFIER_SYSTEM_PROMPT,
            history,
            user_text,
            max_steps=settings.agent_max_steps,
            max_retries=settings.agent_max_retries,
        ):
            if event.type == "done":
                done_data = event.data
            else:
                if event.type == "error":
                    errored = True
                if event.type == "thinking":
                    thinking_parts.append(event.data.get("content", ""))
                if event.type in ("thinking", "text"):
                    raw_parts.append(event.data.get("content", ""))
                # ask_options/start_build 的工具事件不外发：澄清轮次对用户只呈现选项卡片、问答与共识卡片
                if event.type != "tool":
                    yield _sse({"type": event.type, **event.data})
    except _StartBuildInvoked as invoked:
        consensus_summary = invoked.summary
    except _AskOptionsInvoked as invoked:
        clarify_payload = invoked.raw
    except Exception as e:  # noqa: BLE001 — 流式过程中的意外以 error 事件收尾
        _persist_partial_thinking(session_factory, project_id, "clarifier", thinking_parts)
        yield _sse({"type": "error", "detail": f"澄清中断: {e}"})
        return
    except BaseException:
        # 刷新/断流触发的生成器关闭：落库已流出的思考后照旧退出（同工程师流）
        _persist_partial_thinking(session_factory, project_id, "clarifier", thinking_parts)
        raise

    if clarify_payload is None and consensus_summary is None:
        # 模型未调 ask_options 但可能把问题 JSON 写进了 content（部分推理模型会把
        # JSON 开头漏进 think 块、尾部被截断）：恢复出合法清单即升级为卡片路径，
        # 避免半截 JSON 以裸文本气泡呈现；恢复失败才走旧形态文本兼容路径。
        recovered = recover_clarify_payload("".join(raw_parts))
        if recovered is not None:
            clarify_payload = json.dumps(recovered, ensure_ascii=False)

    if clarify_payload is not None:
        # 产出选项式澄清问题：卡片一次携完整清单外发并落库，等待用户点选或输入回答（确认门前置）
        yield _sse({"type": "clarify", "content": clarify_payload})
        try:
            with session_factory() as session:
                thinking_text = "".join(thinking_parts).strip()
                if thinking_text:
                    session.add(
                        Message(
                            project_id=project_id, role="clarifier", kind="thinking", content=thinking_text
                        )
                    )
                session.add(
                    Message(project_id=project_id, role="clarifier", kind="clarify", content=clarify_payload)
                )
                session.commit()
        except Exception as e:  # noqa: BLE001 — 澄清落库失败须明示，不得静默断流
            yield _sse({"type": "error", "detail": f"澄清保存失败: {e}"})
            return
        yield _sse({"type": "done", "text": clarify_payload})
        return

    if consensus_summary is None:
        # 模型未调 start_build：本轮是一次澄清提问，收尾落库后结束（不写文件、不留快照）
        question = done_data.get("text", "") if done_data else ""
        if not question:
            if not errored:
                yield _sse({"type": "error", "detail": "澄清未产出任何内容"})
            return
        try:
            with session_factory() as session:
                thinking_text = "".join(thinking_parts).strip()
                if thinking_text:
                    session.add(
                        Message(
                            project_id=project_id, role="clarifier", kind="thinking", content=thinking_text
                        )
                    )
                session.add(
                    Message(project_id=project_id, role="clarifier", kind="text", content=question)
                )
                session.commit()
        except Exception as e:  # noqa: BLE001 — 澄清落库失败须明示，不得静默断流
            yield _sse({"type": "error", "detail": f"澄清保存失败: {e}"})
            return
        yield _sse({"type": "done", "text": question})
        return

    # 产出需求共识：卡片内容流式外发并落库，等待用户确认后才开始生成（确认门，ADR 0003）
    yield _sse({"type": "consensus", "content": consensus_summary})
    try:
        with session_factory() as session:
            thinking_text = "".join(thinking_parts).strip()
            if thinking_text:
                session.add(
                    Message(
                        project_id=project_id, role="clarifier", kind="thinking", content=thinking_text
                    )
                )
            session.add(
                Message(project_id=project_id, role="clarifier", kind="consensus", content=consensus_summary)
            )
            project_row = session.get(Project, project_id)
            if project_row is not None:
                project_row.updated_at = _utcnow()
            session.commit()
    except Exception as e:  # noqa: BLE001 — 共识落库失败须明示，不得静默断流
        yield _sse({"type": "error", "detail": f"需求共识保存失败: {e}"})
        return
    yield _sse({"type": "done", "text": consensus_summary})


@router.post("/{project_id}/messages")
async def send_message(
    project_id: int,
    body: SendMessageRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = get_owned_project(project_id, user, db)

    settings = request.app.state.settings
    existing_files = _existing_file_paths(db, project_id)
    # 弹窗式澄清答案标记（工单 0020）：答案消息落 kind=clarify_answer，
    # 历史回看据此折叠进问答记录卡；普通消息仍为 text。仅澄清分流语义使用，
    # 引导分支（PRD/执行期）不受标记影响。
    user_kind = "clarify_answer" if body.clarify_answer else "text"

    # 团队模式分流（工单 0017 / ADR 0003）：
    # 历史项目（已有 PRD 消息）保留旧流程：待确认时引导先处理 PRD，已确认后进工程师；
    # 新团队项目走：需求澄清 → 需求规格确认门 → 工单拆解与清单确认门 → 执行（后续工单承接）。
    # 克隆等已有文件的场景与工程师模式完全一致。
    stage = "engineer"
    if project.mode == "team" and _exec_state(db, project_id) == "active":
        # 执行期（工单 0018）：工单正在串行执行或有失败待重试，普通消息以引导拦截，
        # 不调模型不计名额；全部完成后自然回到工程师分流（迭代按次计数）。
        stage = "exec_guide"
    elif project.mode == "team" and not existing_files:
        prd_state = _prd_state(db, project_id)
        if prd_state == "pending":
            stage = "guide"
        elif prd_state == "none":
            # 新流水线：共识未定先澄清；共识已定而规格未定则起草/重新起草规格；
            # 规格待确认时继续发消息视为修改意见，重新起草并取代旧规格。
            if _consensus_state(db, project_id) in ("none", "pending"):
                stage = "clarify"
            elif _spec_state(db, project_id) in ("none", "pending"):
                stage = "spec"
            elif _tickets_state(db, project_id) in ("none", "pending"):
                # 工单 0017：规格确认后自动拆单；待确认时继续发消息视为调整粒度/内容的意见，
                # 重新拆解并取代旧清单；一经确认进入执行期，不再重新澄清/拆单（落到工程师）。
                stage = "break"
    elif not existing_files and _consensus_state(db, project_id) in ("none", "pending"):
        # 工程师模式首建分流（工单 0015 / ADR 0003）：尚无文件时先经需求澄清；
        # 共识待确认时继续发消息视为追加输入，重新澄清并产出新共识。
        stage = "clarify"

    if stage == "guide":
        guidance = (
            "团队模式下已产出待确认的 PRD：请先在 PRD 卡片上确认通过（可附追加意见），"
            "工程师才会开始实现；如需调整需求，请将修改意见随确认一并提出。"
        )
        db.add(Message(project_id=project_id, role="user", kind="text", content=body.content))
        db.add(Message(project_id=project_id, role="system", kind="text", content=guidance))
        db.commit()

        async def guide_stream():
            yield _sse({"type": "text", "content": guidance})
            yield _sse({"type": "done", "text": guidance})

        return _sse_response(guide_stream())

    if stage == "exec_guide":
        failed_seq = db.scalar(
            select(Ticket.seq)
            .where(Ticket.project_id == project_id, Ticket.status == "failed")
            .order_by(Ticket.seq)
            .limit(1)
        )
        if failed_seq is not None:
            guidance = (
                f"工单 {failed_seq} 执行失败：请在工单清单卡片上从该工单重试，"
                "已完成的工单不会重跑。"
            )
        else:
            # 含检查点回滚后的重置场景（工单 0019）：未完成工单待用户手动「继续执行」
            guidance = "仍有未完成工单：请在工单清单卡片上点击「继续执行」，从第一个未完成工单起点继续。"
        db.add(Message(project_id=project_id, role="user", kind="text", content=body.content))
        db.add(Message(project_id=project_id, role="system", kind="text", content=guidance))
        db.commit()

        async def exec_guide_stream():
            yield _sse({"type": "text", "content": guidance})
            yield _sse({"type": "done", "text": guidance})

        return _sse_response(exec_guide_stream())

    # 限流检查在落用户消息之前：被拒请求不产生任何持久化痕迹（工单 0011）。
    # 首建流水线整体只占一个名额（工单 0015 / ADR 0003）：项目尚无消息也无文件时，
    # 首条消息扣一次名额；此后只要仍无文件（澄清续轮等流水线内消息）不再计数；
    # 项目有文件后（首建完成）的迭代消息恢复按次计数。
    # accept 之后、返回流之前的任何异常都必须归还名额，否则全局名额泄漏直至重启。
    charged = bool(existing_files) or not _has_any_message(db, project_id)
    if charged:
        _accept_rate_limit(request, user)
    try:
        user_message = Message(
            project_id=project_id, role="user", kind=user_kind, content=body.content
        )
        db.add(user_message)
        db.commit()
        db.refresh(user_message)

        history = _llm_history(
            db, project_id, before_message_id=user_message.id, window=settings.agent_history_window
        )
    except Exception:
        if charged:
            request.app.state.rate_limiter.release()
        raise

    async def event_stream():
        # 同一项目的生成排队串行，避免并发覆盖项目目录与快照 rev 竞态（工单 0007 评审项）
        async with _project_lock(project_id):
            if stage == "clarify":
                async for chunk in _clarify_stream(request, project_id, body.content, history):
                    yield chunk
            elif stage == "spec":
                async for chunk in _spec_stream(request, project_id, body.content, history):
                    yield chunk
            elif stage == "break":
                async for chunk in _break_stream(request, project_id, body.content, history):
                    yield chunk
            else:
                async for chunk in _engineer_stream(
                    request, project_id, body.content, history
                ):
                    yield chunk

    stream = event_stream()
    if charged:
        stream = _limited_stream(stream, request)
    return _sse_response(stream)


@router.post("/{project_id}/prd/confirm")
async def confirm_prd(
    project_id: int,
    body: ConfirmPrdRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认待确认的 PRD（工单 0010）：确认后工程师智能体随即开始实现（SSE 流）。

    确认与追加意见落对话历史（role=user, kind=prd_confirm），重新打开可回看。
    """
    project = get_owned_project(project_id, user, db)
    if project.mode != "team":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="工程师模式项目没有 PRD 流程"
        )
    if _prd_state(db, project_id) != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前没有待确认的 PRD"
        )

    feedback = body.feedback.strip()
    confirm_content = feedback if feedback else "确认通过，开始实现。"
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory

    # 确认即触发工程师生成：同样受限流约束，超限不落确认消息（工单 0011）。
    # 前置校验（属主/模式/状态）已在上方完成，accept 与返回流之间无可抛语句，
    # 名额由 _limited_stream 的 finally 归还。
    _accept_rate_limit(request, user)

    async def event_stream():
        async with _project_lock(project_id):
            # 锁内复查状态并落确认：并发双击/重试不会造成重复确认与两次生成（工单 0010）
            with session_factory() as session:
                if _prd_state(session, project_id) != "pending":
                    yield _sse({"type": "error", "detail": "当前没有待确认的 PRD"})
                    return
                confirm_message = Message(
                    project_id=project_id,
                    role="user",
                    kind="prd_confirm",
                    content=confirm_content,
                )
                session.add(confirm_message)
                session.commit()
                session.refresh(confirm_message)
                history = _llm_history(
                    session,
                    project_id,
                    before_message_id=confirm_message.id,
                    window=settings.agent_history_window,
                )
            async for chunk in _engineer_stream(
                request, project_id, confirm_content, history
            ):
                yield chunk

    return _sse_response(_limited_stream(event_stream(), request))


@router.post("/{project_id}/consensus/confirm")
async def confirm_consensus(
    project_id: int,
    body: ConfirmConsensusRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认待确认的需求共识（工单 0015）：确认后随即进入下一阶段（SSE 流）。

    工程师模式确认后工程师智能体开始生成；团队模式确认后需求规格智能体开始
    起草需求规格（工单 0016）。确认与修改意见落对话历史（role=user,
    kind=consensus_confirm），重新打开可回看。首建流水线内不再占用新名额。
    """
    project = get_owned_project(project_id, user, db)
    if _consensus_state(db, project_id) != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前没有待确认的需求共识"
        )

    is_team = project.mode == "team"
    feedback = body.feedback.strip()
    default_text = "确认共识，开始起草需求规格。" if is_team else "确认共识，开始生成。"
    confirm_content = feedback if feedback else default_text
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory

    async def event_stream():
        async with _project_lock(project_id):
            # 锁内复查状态并落确认：并发双击/重试不会造成重复确认与两次生成（同 PRD 确认）
            with session_factory() as session:
                if _consensus_state(session, project_id) != "pending":
                    yield _sse({"type": "error", "detail": "当前没有待确认的需求共识"})
                    return
                confirm_message = Message(
                    project_id=project_id,
                    role="user",
                    kind="consensus_confirm",
                    content=confirm_content,
                )
                session.add(confirm_message)
                session.commit()
                session.refresh(confirm_message)
                history = _llm_history(
                    session,
                    project_id,
                    before_message_id=confirm_message.id,
                    window=settings.agent_history_window,
                )
            if is_team:
                # 团队模式：共识确认后进入需求规格阶段（工单 0016）
                async for chunk in _spec_stream(request, project_id, confirm_content, history):
                    yield chunk
            else:
                async for chunk in _engineer_stream(
                    request, project_id, confirm_content, history
                ):
                    yield chunk

    return _sse_response(event_stream())


@router.post("/{project_id}/spec/confirm")
async def confirm_spec(
    project_id: int,
    body: ConfirmSpecRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认待确认的需求规格（工单 0016）：确认后拆单智能体随即开始拆解（SSE 流）。

    确认与修改意见落对话历史（role=user, kind=spec_confirm）。
    首建流水线内不再占用新名额（名额在首条消息时已扣，ADR 0003）。
    """
    project = get_owned_project(project_id, user, db)
    if project.mode != "team":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="工程师模式项目没有需求规格流程"
        )
    if _spec_state(db, project_id) != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前没有待确认的需求规格"
        )

    feedback = body.feedback.strip()
    confirm_content = feedback if feedback else "确认规格，开始拆解工单。"
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory

    async def event_stream():
        async with _project_lock(project_id):
            # 锁内复查状态并落确认：并发双击/重试不会造成重复确认与两次生成（同共识确认）
            with session_factory() as session:
                if _spec_state(session, project_id) != "pending":
                    yield _sse({"type": "error", "detail": "当前没有待确认的需求规格"})
                    return
                confirm_message = Message(
                    project_id=project_id,
                    role="user",
                    kind="spec_confirm",
                    content=confirm_content,
                )
                session.add(confirm_message)
                session.commit()
                session.refresh(confirm_message)
                history = _llm_history(
                    session,
                    project_id,
                    before_message_id=confirm_message.id,
                    window=settings.agent_history_window,
                )
                existing_files = _existing_file_paths(session, project_id)
            # 规格确认后进入拆单阶段（工单 0017）；克隆等已有文件的团队项目跳过拆单直接实现（同跳过 PRD）
            if existing_files:
                async for chunk in _engineer_stream(
                    request, project_id, confirm_content, history
                ):
                    yield chunk
            else:
                async for chunk in _break_stream(request, project_id, confirm_content, history):
                    yield chunk

    return _sse_response(event_stream())


def _ticket_payload(row: Ticket) -> dict:
    """工单表行转卡片字段（blocked_by 从 JSON 解出序号列表）。

    检查点快照以版本号呈现（工单 0018），快照行已删（清理超限）时降级为 None。
    """
    payload = {
        "seq": row.seq,
        "title": row.title,
        "deliverable": row.deliverable,
        "status": row.status,
        "snapshot_rev": row.snapshot.rev if row.snapshot is not None else None,
    }
    try:
        payload["blocked_by"] = json.loads(row.blocked_by or "[]")
    except ValueError:
        payload["blocked_by"] = []
    return payload


@router.get("/{project_id}/tickets", response_model=list[TicketOut])
def list_tickets(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """当前工单清单（工单 0017）：按序号升序，刷新页面可回看。"""
    get_owned_project(project_id, user, db)
    return [
        _ticket_payload(row)
        for row in db.scalars(
            select(Ticket).where(Ticket.project_id == project_id).order_by(Ticket.seq)
        )
    ]


@router.post("/{project_id}/tickets/confirm")
async def confirm_tickets(
    project_id: int,
    body: ConfirmTicketsRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认待确认的工单清单（工单 0017）：确认后进入执行期（SSE 流）。

    执行由工程师智能体按检查点串行承接（工单 0018）：按依赖序逐单执行，
    每单完成形成检查点快照，失败可从该单重试（见 /tickets/resume）；
    确认后不可重新澄清、不可重新拆单（分流已拦截）。
    确认与调整意见落对话历史（role=user, kind=tickets_confirm）。
    首建流水线内不再占用新名额（名额在首条消息时已扣，ADR 0003）。
    """
    project = get_owned_project(project_id, user, db)
    if project.mode != "team":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="工程师模式项目没有工单流程"
        )
    if _tickets_state(db, project_id) != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前没有待确认的工单清单"
        )

    feedback = body.feedback.strip()
    confirm_content = feedback if feedback else "确认工单清单，开始执行。"
    session_factory = request.app.state.session_factory

    async def event_stream():
        async with _project_lock(project_id):
            # 锁内复查状态并落确认：并发双击/重试不会造成重复确认与两次生成（同规格确认）
            with session_factory() as session:
                if _tickets_state(session, project_id) != "pending":
                    yield _sse({"type": "error", "detail": "当前没有待确认的工单清单"})
                    return
                session.add(
                    Message(
                        project_id=project_id,
                        role="user",
                        kind="tickets_confirm",
                        content=confirm_content,
                    )
                )
                session.commit()
            # 检查点串行执行（工单 0018）：逐单交付，每单一个检查点快照，失败可重试单张工单；
            # 确认消息已落库，自然进入执行上下文（调整意见随历史可见）
            async for chunk in _exec_tickets_stream(request, project_id):
                yield chunk

    return _sse_response(event_stream())


@router.post("/{project_id}/tickets/resume")
async def resume_tickets(
    project_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """继续/重试工单执行（工单 0018）：从第一张未完成工单起点继续（SSE 流）。

    覆盖两种场景：某单失败后从该单重试（已完成的单不重跑）；断线/刷新后
    重连继续（中断遗留的 running 单重新执行）。全部完成后返回 409：
    项目已进入常规迭代，直接发消息即可。首建流水线内不重复占用名额（ADR 0003）。
    """
    project = get_owned_project(project_id, user, db)
    if project.mode != "team":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="工程师模式项目没有工单流程"
        )
    if _exec_state(db, project_id) != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前没有可继续的工单执行"
        )

    async def event_stream():
        async with _project_lock(project_id):
            # 锁内复查：并发双击/与确认竞态不会造成两路执行（同确认门范式）
            with request.app.state.session_factory() as session:
                if _exec_state(session, project_id) != "active":
                    yield _sse({"type": "error", "detail": "当前没有可继续的工单执行"})
                    return
            async for chunk in _exec_tickets_stream(request, project_id):
                yield chunk

    return _sse_response(event_stream())


@router.get("/{project_id}/files", response_model=list[FileOut])
def list_files(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ProjectFile]:
    get_owned_project(project_id, user, db)
    return list(
        db.scalars(
            select(ProjectFile)
            .where(ProjectFile.project_id == project_id)
            .order_by(ProjectFile.path)
        )
    )


@router.get("/{project_id}/files/{file_path:path}", response_model=FileContentOut)
def read_file_content(
    project_id: int,
    file_path: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """属主读取当前版本单个文件内容（代码视图用，工单 0007）。"""
    get_owned_project(project_id, user, db)
    # resolve 后再 relative_to：storage_root 可为相对路径，直接相比会在 Windows 上抛错
    root = project_dir(request, project_id).resolve()
    try:
        target = resolve_sandboxed(root, file_path)
    except SandboxViolation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    content = target.read_text(encoding="utf-8")
    return {
        "path": target.relative_to(root).as_posix(),
        "size": len(content.encode("utf-8")),
        "content": content,
    }


# --- 版本快照与回滚（工单 0007） ---
#
# 快照在每次成功生成后自动留档（见 send_message 收尾），此处提供浏览与回滚。
# 回滚直接恢复项目目录文件，不产生新快照；后续迭代以恢复后的基线继续。


def _get_owned_snapshot(db: Session, project_id: int, snapshot_id: int) -> Snapshot:
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="快照不存在")
    return snapshot


def _checkpoint_seq_map(db: Session, project_id: int) -> dict[int, int]:
    """快照 id → 所属检查点工单序号（工单 0019）：快照列表标注检查点来源。

    按版本区间归属：检查点版本不晚于某工单当前检查点的最早工单即其所属。
    续跑形成的新检查点取代旧引用后，旧版本快照仍保留来源标注，
    回滚到旧检查点时用户仍能看到重置提示（与后端重置语义一致）。
    """
    checkpoint_revs = [
        (row.seq, row.snapshot.rev)
        for row in db.scalars(
            select(Ticket)
            .where(Ticket.project_id == project_id, Ticket.snapshot_id.isnot(None))
            .order_by(Ticket.seq)
        )
        if row.snapshot is not None
    ]
    result: dict[int, int] = {}
    for snapshot in db.scalars(
        select(Snapshot).where(Snapshot.project_id == project_id)
    ):
        for seq, rev in checkpoint_revs:
            if snapshot.rev <= rev:
                result[snapshot.id] = seq
                break
    return result


def _reset_tickets_after_checkpoint(db: Session, project_id: int, rev: int) -> None:
    """回滚到检查点后，把该检查点之后的工单重置为未完成（工单 0019）。

    未完成工单（失败待重试、断流遗留 running）一律归位 open；已完成工单的检查点
    版本大于回滚目标即随之失效，重置 open 并清除引用；不晚于目标的检查点保持有效。
    续跑经 /tickets/resume 从第一个未完成工单起点继续（序号升序即依赖序）。
    """
    for row in db.scalars(select(Ticket).where(Ticket.project_id == project_id)):
        if row.status != "done" or (row.snapshot is not None and row.snapshot.rev > rev):
            row.status = "open"
            row.snapshot_id = None


@router.get("/{project_id}/snapshots", response_model=list[SnapshotOut])
def list_snapshots(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """版本历史：按 rev 倒序（最新版本在前）；检查点快照标注来源工单序号（工单 0019）。"""
    get_owned_project(project_id, user, db)
    checkpoint_seqs = _checkpoint_seq_map(db, project_id)
    return [
        SnapshotOut.model_validate(s).model_dump(mode="json")
        | {"ticket_seq": checkpoint_seqs.get(s.id)}
        for s in db.scalars(
            select(Snapshot)
            .where(Snapshot.project_id == project_id)
            .order_by(Snapshot.rev.desc())
        )
    ]


@router.get("/{project_id}/snapshots/{snapshot_id}", response_model=SnapshotDetailOut)
def get_snapshot(
    project_id: int,
    snapshot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    get_owned_project(project_id, user, db)
    snapshot = _get_owned_snapshot(db, project_id, snapshot_id)
    payload = SnapshotOut.model_validate(snapshot).model_dump(mode="json")
    payload["ticket_seq"] = _checkpoint_seq_map(db, project_id).get(snapshot.id)
    payload["files"] = [
        {"path": path, "size": size}
        for path, size in list_snapshot_files(project_dir(request, project_id), snapshot)
    ]
    return payload


@router.get("/{project_id}/snapshots/{snapshot_id}/diff", response_model=SnapshotDiffOut)
def get_snapshot_diff(
    project_id: int,
    snapshot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """安全网（Layer 6）：该快照相对前一版的文件级差异，供前端 diff 面板展开逐文件核对。"""
    get_owned_project(project_id, user, db)
    snapshot = _get_owned_snapshot(db, project_id, snapshot_id)
    base = db.scalar(
        select(Snapshot).where(Snapshot.project_id == project_id, Snapshot.rev == snapshot.rev - 1)
    )
    return {
        "base_rev": base.rev if base is not None else None,
        "target_rev": snapshot.rev,
        "files": diff_snapshot(project_dir(request, project_id), base, snapshot),
    }


@router.post("/{project_id}/snapshots/{snapshot_id}/rollback", response_model=SnapshotOut)
def rollback_snapshot(
    project_id: int,
    snapshot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Snapshot:
    """把当前文件恢复为该快照状态；后续迭代以其为基线。

    团队模式检查点回滚（工单 0019）：该检查点之后的工单重置为未完成，由用户手动
    「继续执行」从第一个未完成工单续跑（续跑不占新名额）；有工单执行中（running）
    时拒绝回滚，避免与串行执行流竞态覆盖文件。
    """
    get_owned_project(project_id, user, db)
    snapshot = _get_owned_snapshot(db, project_id, snapshot_id)
    project = db.get(Project, project_id)
    if project is not None and project.mode == "team":
        running = db.scalar(
            select(Ticket.id)
            .where(Ticket.project_id == project_id, Ticket.status == "running")
            .limit(1)
        )
        if running is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="工单执行中，请完成或重试后再回滚"
            )
    root = project_dir(request, project_id)
    restore_snapshot(root, snapshot)
    _sync_file_index(db, project_id, root)
    if project is not None:
        if project.mode == "team":
            _reset_tickets_after_checkpoint(db, project_id, snapshot.rev)
        project.updated_at = _utcnow()
    db.commit()
    db.refresh(snapshot)
    return snapshot


# --- 预览托管（工单 0005）：属主项目的当前版本文件按真实 MIME 类型提供 ---
#
# 鉴权只靠登录时写入的 Cookie atoms_token（见 routers/auth.py）：
# iframe 与其子资源同源请求自动携带，无需在 URL 里暴露令牌。
# 越界路径与白名单外扩展名由共享的 serving.serve_project_file 拦截。


def _preview_user(request: Request, db: Session) -> User | None:
    """按登录 Cookie 解析预览访问者；无 Cookie 或令牌无效返回 None。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return resolve_user_by_token(request.app.state.settings, token, db)


def _serve_preview(request: Request, project_id: int, rel_path: str, db: Session) -> Response:
    user = _preview_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期")
    project = db.get(Project, project_id)
    # 非属主与不存在的项目同返 404，不泄露项目归属
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return serve_project_file(project_dir(request, project_id), rel_path)


@router.get("/{project_id}/preview")
def preview_root(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return _serve_preview(request, project_id, "index.html", db)


@router.get("/{project_id}/preview/{file_path:path}")
def preview_file(
    project_id: int,
    file_path: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return _serve_preview(request, project_id, file_path.strip("/") or "index.html", db)


def _persist_event(session_factory, project_id: int, data: dict) -> None:
    """工具事件即时落库，刷新页面可回看完整过程。"""
    with session_factory() as session:
        session.add(
            Message(
                project_id=project_id,
                role="engineer",
                kind="event",
                content=json.dumps(data, ensure_ascii=False),
            )
        )
        session.commit()


def _persist_partial_thinking(
    session_factory, project_id: int, role: str, thinking_parts: list[str]
) -> None:
    """生成未正常收尾（刷新/断流或出错）时，把已流出的思考原样落库。

    否则刷新后思考过程凭空消失，且消息尾停在工具事件行，无法区分中断与完成；
    收尾落库不含最终结论：半截正文不得伪装成结论（诊断修复）。
    """
    thinking_text = "".join(thinking_parts).strip()
    if not thinking_text:
        return
    with session_factory() as session:
        session.add(
            Message(project_id=project_id, role=role, kind="thinking", content=thinking_text)
        )
        session.commit()
