"""项目 CRUD、对话历史、生成消息（SSE 流式）、预览托管。"""

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..agent.loop import run_generation
from ..agent.prompts import build_system_prompt
from ..agent.tools import (
    FileSandbox,
    SandboxViolation,
    build_tools,
    execute_tool,
    resolve_sandboxed,
)
from ..deps import COOKIE_NAME, get_current_user, get_db, resolve_user_by_token
from ..models import Message, Project, ProjectFile, Publication, Snapshot, User, _utcnow
from ..schemas import (
    CreateProjectRequest,
    FileContentOut,
    FileOut,
    MessageOut,
    ProjectOut,
    SendMessageRequest,
    SnapshotDetailOut,
    SnapshotOut,
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
    db.execute(delete(Message).where(Message.project_id == project_id))
    db.execute(delete(ProjectFile).where(ProjectFile.project_id == project_id))
    db.execute(delete(Publication).where(Publication.project_id == project_id))
    db.execute(delete(Snapshot).where(Snapshot.project_id == project_id))
    db.delete(project)
    db.commit()
    shutil.rmtree(project_dir(request, project_id), ignore_errors=True)


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
    """
    rows = db.scalars(
        select(Message)
        .where(Message.project_id == project_id, Message.id < before_message_id)
        .order_by(Message.id)
    )
    history = []
    for m in rows:
        if m.kind != "text":
            continue
        if m.role == "user":
            history.append(HumanMessage(content=m.content))
        else:
            history.append(AIMessage(content=m.content))
    if window > 0:
        history = history[-2 * window :]
    return history


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


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/{project_id}/messages")
async def send_message(
    project_id: int,
    body: SendMessageRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = get_owned_project(project_id, user, db)
    user_message = Message(project_id=project_id, role="user", kind="text", content=body.content)
    db.add(user_message)
    db.commit()
    db.refresh(user_message)

    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    history = _llm_history(
        db, project_id, before_message_id=user_message.id, window=settings.agent_history_window
    )
    existing_files = [
        p
        for p in db.scalars(
            select(ProjectFile.path)
            .where(ProjectFile.project_id == project_id)
            .order_by(ProjectFile.path)
        )
    ]

    async def event_stream():
        # 同一项目的生成排队串行，避免并发覆盖项目目录与快照 rev 竞态（工单 0007 评审项）
        async with _project_lock(project_id):
            async for chunk in generate_stream():
                yield chunk

    async def generate_stream():
        try:
            model = request.app.state.model_factory(settings)
        except Exception as e:  # noqa: BLE001 — 未配置 Key 等环境问题以 error 事件收尾
            yield _sse({"type": "error", "detail": str(e)})
            return

        sandbox = FileSandbox(project_dir(request, project_id))
        tools = build_tools(sandbox)
        system_prompt = build_system_prompt(existing_files)

        done_data: dict | None = None
        try:
            async for event in run_generation(
                model,
                tools,
                execute_tool,
                system_prompt,
                history,
                body.content,
                max_steps=settings.agent_max_steps,
                max_retries=settings.agent_max_retries,
            ):
                if event.type == "done":
                    # done 先扣下：落盘完成后才外发，保证它是流的最后一个事件
                    done_data = event.data
                else:
                    yield _sse({"type": event.type, **event.data})
                    if event.type == "tool" and event.data.get("status") != "start":
                        _persist_event(session_factory, project_id, event.data)
        except Exception as e:  # noqa: BLE001 — 流式过程中的意外以 error 事件收尾
            yield _sse({"type": "error", "detail": f"生成中断: {e}"})
            return

        try:
            with session_factory() as session:
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

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
