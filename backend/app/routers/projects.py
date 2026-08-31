"""项目 CRUD、对话历史、生成消息（SSE 流式）。"""

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..agent.loop import run_generation
from ..agent.prompts import build_system_prompt
from ..agent.tools import ALLOWED_EXTENSIONS, FileSandbox, build_tools, execute_tool
from ..deps import get_current_user, get_db
from ..models import Message, Project, ProjectFile, User, _utcnow
from ..schemas import (
    CreateProjectRequest,
    MessageOut,
    ProjectOut,
    SendMessageRequest,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def project_dir(request: Request, project_id: int) -> Path:
    return Path(request.app.state.settings.storage_root) / "projects" / str(project_id)


def get_owned_project(project_id: int, user: User, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: CreateProjectRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Project:
    project = Project(user_id=user.id, name=body.name, mode=body.mode)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[Project]:
    return list(
        db.scalars(
            select(Project).where(Project.user_id == user.id).order_by(Project.updated_at.desc())
        )
    )


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Project:
    return get_owned_project(project_id, user, db)


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
    """以磁盘为准刷新文件索引表。"""
    on_disk: dict[str, int] = {}
    if root.is_dir():
        for f in root.rglob("*"):
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
                on_disk[f.relative_to(root).as_posix()] = f.stat().st_size
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

        with session_factory() as session:
            final_text = done_data.get("text", "") if done_data else ""
            if final_text:
                session.add(
                    Message(project_id=project_id, role="engineer", kind="text", content=final_text)
                )
            _sync_file_index(session, project_id, sandbox.root)
            project_row = session.get(Project, project_id)
            if project_row is not None:
                project_row.updated_at = _utcnow()
            session.commit()
        if done_data is not None:
            yield _sse({"type": "done", **done_data})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
