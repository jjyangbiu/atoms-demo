"""项目 CRUD、对话历史、生成消息（SSE 流式）、预览托管。"""

import asyncio
import json
import math
import shutil
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
    execute_tool,
    parse_ticket_payload,
    resolve_sandboxed,
)
from ..deps import COOKIE_NAME, get_current_user, get_db, resolve_user_by_token
from ..models import Message, Project, ProjectFile, Publication, Snapshot, Ticket, User, _utcnow
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
    SnapshotOut,
    TicketOut,
)
from ..snapshots import (
    create_snapshot,
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
        if m.kind in ("event", "thinking") or m.role == "system":
            # 工具事件行、思考过程行与引导性系统消息不入上下文（工单 0010）
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
    """项目当前文件索引的路径清单（按路径排序），供系统提示与分流判断共用。"""
    return list(
        db.scalars(
            select(ProjectFile.path)
            .where(ProjectFile.project_id == project_id)
            .order_by(ProjectFile.path)
        )
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


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


async def _engineer_stream(
    request: Request, project_id: int, user_text: str, history: list, existing_files: list[str]
):
    """工程师智能体生成流（SSE 块）：工程师模式与团队模式确认后共用（工单 0010）。"""
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    try:
        model = request.app.state.model_factory(settings)
    except Exception as e:  # noqa: BLE001 — 未配置 Key 等环境问题以 error 事件收尾
        yield _sse({"type": "error", "detail": str(e)})
        return

    sandbox = FileSandbox(project_dir(request, project_id))
    # 知识库可用时附带 search_templates 检索工具（工单 0009）；不可用时降级为纯文件工具
    tools = build_tools(sandbox, maybe_knowledge_store(request.app))
    system_prompt = build_system_prompt(existing_files)

    done_data: dict | None = None
    thinking_parts: list[str] = []
    try:
        async for event in run_generation(
            model,
            tools,
            execute_tool,
            system_prompt,
            history,
            user_text,
            max_steps=settings.agent_max_steps,
            max_retries=settings.agent_max_retries,
        ):
            if event.type == "done":
                # done 先扣下：落盘完成后才外发，保证它是流的最后一个事件
                done_data = event.data
            else:
                if event.type == "thinking":
                    # 思考增量另存一份：正常收尾合并落库；中断时也据已流出部分落库（诊断修复）
                    thinking_parts.append(event.data.get("content", ""))
                yield _sse({"type": event.type, **event.data})
                if event.type == "tool" and event.data.get("status") != "start":
                    _persist_event(session_factory, project_id, event.data)
    except Exception as e:  # noqa: BLE001 — 流式过程中的意外以 error 事件收尾，思考已流出部分仍落库（诊断修复）
        _persist_partial_thinking(session_factory, project_id, "engineer", thinking_parts)
        yield _sse({"type": "error", "detail": f"生成中断: {e}"})
        return
    except BaseException:
        # 刷新/断流触发的生成器关闭（GeneratorExit/CancelledError）：
        # 不能在此 yield（已关闭），只把已流出的思考落库后照旧退出（诊断修复）
        _persist_partial_thinking(session_factory, project_id, "engineer", thinking_parts)
        raise

    try:
        with session_factory() as session:
            thinking_text = "".join(thinking_parts).strip()
            if thinking_text:
                session.add(
                    Message(project_id=project_id, role="engineer", kind="thinking", content=thinking_text)
                )
            final_text = done_data.get("text", "") if done_data else ""
            if final_text:
                session.add(
                    Message(project_id=project_id, role="engineer", kind="text", content=final_text)
                )
            _sync_file_index(session, project_id, sandbox.root)
            # 每次成功生成（首轮与迭代）自动留档一版快照；失败的生成不留档（工单 0007）
            if done_data is not None:
                create_snapshot(session, project_id, sandbox.root, settings.snapshot_max_kept)
            project_row = session.get(Project, project_id)
            if project_row is not None:
                project_row.updated_at = _utcnow()
            session.commit()
    except Exception as e:  # noqa: BLE001 — 收尾落盘失败也须以 error 事件告知，不得静默断流
        yield _sse({"type": "error", "detail": f"生成收尾失败: {e}"})
        return
    if done_data is not None:
        yield _sse({"type": "done", **done_data})


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


class _StartBuildInvoked(Exception):
    """澄清智能体调用 start_build 的控制流信号：携需求共识摘要立即终止循环。"""

    def __init__(self, summary: str):
        self.summary = summary
        super().__init__("start_build")


async def _clarify_stream(request: Request, project_id: int, user_text: str, history: list):
    """需求澄清流（工单 0015）：分轮问答直至无未决问题，产出需求共识卡片。

    澄清智能体唯一工具是 start_build（无任何文件工具）：
    - 模型返回纯文本 → 澄清提问，以 role=clarifier 落历史；
    - 模型调用 start_build → 需求共识以 consensus 事件流式外发并落库，本轮结束；
      start_build 的 ToolMessage 不回传模型，避免模型在共识后继续输出。
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
        return execute_tool(tools, name, args)

    consensus_summary: str | None = None
    done_data: dict | None = None
    errored = False
    thinking_parts: list[str] = []
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
                # start_build 的工具事件不外发：澄清轮次对用户只呈现问答与共识卡片
                if event.type != "tool":
                    yield _sse({"type": event.type, **event.data})
    except _StartBuildInvoked as invoked:
        consensus_summary = invoked.summary
    except Exception as e:  # noqa: BLE001 — 流式过程中的意外以 error 事件收尾
        _persist_partial_thinking(session_factory, project_id, "clarifier", thinking_parts)
        yield _sse({"type": "error", "detail": f"澄清中断: {e}"})
        return
    except BaseException:
        # 刷新/断流触发的生成器关闭：落库已流出的思考后照旧退出（同工程师流）
        _persist_partial_thinking(session_factory, project_id, "clarifier", thinking_parts)
        raise

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

    # 团队模式分流（工单 0017 / ADR 0003）：
    # 历史项目（已有 PRD 消息）保留旧流程：待确认时引导先处理 PRD，已确认后进工程师；
    # 新团队项目走：需求澄清 → 需求规格确认门 → 工单拆解与清单确认门 → 执行（后续工单承接）。
    # 克隆等已有文件的场景与工程师模式完全一致。
    stage = "engineer"
    if project.mode == "team" and not existing_files:
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
            project_id=project_id, role="user", kind="text", content=body.content
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
                    request, project_id, body.content, history, existing_files
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
                existing_files = _existing_file_paths(session, project_id)
            async for chunk in _engineer_stream(
                request, project_id, confirm_content, history, existing_files
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
                existing_files = _existing_file_paths(session, project_id)
            if is_team:
                # 团队模式：共识确认后进入需求规格阶段（工单 0016）
                async for chunk in _spec_stream(request, project_id, confirm_content, history):
                    yield chunk
            else:
                async for chunk in _engineer_stream(
                    request, project_id, confirm_content, history, existing_files
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
                    request, project_id, confirm_content, history, existing_files
                ):
                    yield chunk
            else:
                async for chunk in _break_stream(request, project_id, confirm_content, history):
                    yield chunk

    return _sse_response(event_stream())


def _ticket_payload(row: Ticket) -> dict:
    """工单表行转卡片字段（blocked_by 从 JSON 解出序号列表）。"""
    payload = {
        "seq": row.seq,
        "title": row.title,
        "deliverable": row.deliverable,
        "status": row.status,
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

    本工单范围内执行由工程师智能体承接（检查点串行执行由后续工单承接）；
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
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory

    async def event_stream():
        async with _project_lock(project_id):
            # 锁内复查状态并落确认：并发双击/重试不会造成重复确认与两次生成（同规格确认）
            with session_factory() as session:
                if _tickets_state(session, project_id) != "pending":
                    yield _sse({"type": "error", "detail": "当前没有待确认的工单清单"})
                    return
                confirm_message = Message(
                    project_id=project_id,
                    role="user",
                    kind="tickets_confirm",
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
                # 工单清单以执行目标的形式附入工程师上下文（清单行本身不入上下文）
                ticket_lines = [
                    f"{p['seq']}. {p['title']}：{p['deliverable']}"
                    + (f"（被工单 {', '.join(str(b) for b in p['blocked_by'])} 阻塞）" if p["blocked_by"] else "")
                    for p in map(
                        _ticket_payload,
                        session.scalars(
                            select(Ticket)
                            .where(Ticket.project_id == project_id)
                            .order_by(Ticket.seq)
                        ),
                    )
                ]
            if ticket_lines:
                confirm_content_with_list = (
                    f"{confirm_content}\n\n请按工单清单执行：\n" + "\n".join(ticket_lines)
                )
            else:
                confirm_content_with_list = confirm_content
            async for chunk in _engineer_stream(
                request, project_id, confirm_content_with_list, history, existing_files
            ):
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


@router.get("/{project_id}/snapshots", response_model=list[SnapshotOut])
def list_snapshots(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Snapshot]:
    """版本历史：按 rev 倒序（最新版本在前）。"""
    get_owned_project(project_id, user, db)
    return list(
        db.scalars(
            select(Snapshot)
            .where(Snapshot.project_id == project_id)
            .order_by(Snapshot.rev.desc())
        )
    )


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
    payload["files"] = [
        {"path": path, "size": size}
        for path, size in list_snapshot_files(project_dir(request, project_id), snapshot)
    ]
    return payload


@router.post("/{project_id}/snapshots/{snapshot_id}/rollback", response_model=SnapshotOut)
def rollback_snapshot(
    project_id: int,
    snapshot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Snapshot:
    """把当前文件恢复为该快照状态；后续迭代以其为基线。"""
    get_owned_project(project_id, user, db)
    snapshot = _get_owned_snapshot(db, project_id, snapshot_id)
    root = project_dir(request, project_id)
    restore_snapshot(root, snapshot)
    _sync_file_index(db, project_id, root)
    project = db.get(Project, project_id)
    if project is not None:
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
