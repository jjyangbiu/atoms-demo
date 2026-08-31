"""App 世界画廊与克隆（工单 0008）：所有已发布应用汇入公开画廊。

- 匿名浏览列表与详情：标题、描述、作者、实时运行预览链接（/p/{slug}）
- 登录用户一键克隆：文件与元数据复制为克隆者名下的新项目，立即可继续迭代
- 克隆为物理拷贝且不带发布记录：原项目后续演进、下架乃至删除都不影响副本
- 语义搜索（工单 0009）：?q= 按意图匹配已发布应用，非关键词精确匹配；
  知识库不可用时降级为标题/描述关键词包含匹配
"""

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
from ..models import Message, Project, Publication, User
from ..rag.store import maybe_knowledge_store
from ..schemas import ProjectOut, WorldAppOut
from ..snapshots import iter_project_files
from .projects import _sync_file_index, project_dir, project_payload

router = APIRouter(prefix="/api/world", tags=["world"])

# 画廊卡片上的描述截断上限（描述取创建者首次诉求，见 _description）
_DESCRIPTION_MAX = 120
# 语义搜索相似度下限：过滤明显无关结果（工单 0009）
_SEARCH_MIN_SCORE = 0.1


def _get_published(db: Session, slug: str) -> tuple[Publication, Project]:
    """按 slug 取活跃发布与其项目；不存在或已下架返回 404。"""
    publication = db.scalar(select(Publication).where(Publication.slug == slug))
    project = db.get(Project, publication.project_id) if publication is not None else None
    if publication is None or project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="应用不存在或已下架")
    return publication, project


def _description(db: Session, project_id: int) -> str:
    """以创建者的首次诉求作为应用描述；无对话记录时返回空串。"""
    first_ask = db.scalar(
        select(Message.content)
        .where(Message.project_id == project_id, Message.role == "user", Message.kind == "text")
        .order_by(Message.id)
        .limit(1)
    )
    if not first_ask:
        return ""
    first_ask = first_ask.strip()
    return first_ask[:_DESCRIPTION_MAX]


def _world_entry(db: Session, publication: Publication, project: Project) -> WorldAppOut:
    author = db.scalar(select(User.username).where(User.id == project.user_id)) or "未知用户"
    return WorldAppOut(
        slug=publication.slug,
        title=project.name,
        description=_description(db, project.id),
        author=author,
        preview_url=f"/p/{publication.slug}",
        published_at=publication.created_at,
        official=publication.official,
    )


def _all_entries(db: Session) -> list[WorldAppOut]:
    """全部已发布应用，最新发布在前。"""
    entries = []
    for publication in db.scalars(
        select(Publication).order_by(Publication.created_at.desc(), Publication.id.desc())
    ):
        project = db.get(Project, publication.project_id)
        if project is None:
            continue
        entries.append(_world_entry(db, publication, project))
    return entries


@router.get("", response_model=list[WorldAppOut])
def list_world(
    request: Request, q: str | None = None, db: Session = Depends(get_db)
) -> list[WorldAppOut]:
    """画廊列表：所有已发布应用，最新发布在前；任何人无需登录。

    带 q 时为语义搜索（工单 0009）：按意图命中相关应用，相似度降序。
    """
    if q and q.strip():
        return _search_world(request.app, db, q.strip())
    return _all_entries(db)


def _search_world(app, db: Session, query: str) -> list[WorldAppOut]:
    """知识库可用时语义检索已发布应用；构建失败或运行期故障都降级为关键词包含匹配。"""
    hits = None
    store = maybe_knowledge_store(app)
    if store is not None:
        try:
            hits = store.search(query, top_k=50, source="published")
        except Exception:  # noqa: BLE001 — embedding/向量库运行期异常不阻断公开画廊搜索
            hits = None
    if hits is None:
        needle = query.lower()
        return [
            e
            for e in _all_entries(db)
            if needle in e.title.lower() or needle in e.description.lower()
        ]
    entries = []
    for hit in hits:
        if hit["score"] < _SEARCH_MIN_SCORE or not hit.get("slug"):
            continue
        publication = db.scalar(select(Publication).where(Publication.slug == hit["slug"]))
        project = db.get(Project, publication.project_id) if publication is not None else None
        # 知识库里可能有已下架/已删除的残留条目，以 DB 为准过滤（相似度降序不变）
        if publication is None or project is None:
            continue
        entries.append(_world_entry(db, publication, project))
    return entries


@router.get("/{slug}", response_model=WorldAppOut)
def world_detail(slug: str, db: Session = Depends(get_db)) -> WorldAppOut:
    """画廊详情：单个已发布应用的卡片信息；任何人无需登录。"""
    publication, project = _get_published(db, slug)
    return _world_entry(db, publication, project)


def _abort_clone(db: Session, project: Project, target_root: Path) -> None:
    """克隆中途失败时清理已建项目行与目录，不留下孤儿项目。"""
    db.delete(project)
    db.commit()
    shutil.rmtree(target_root, ignore_errors=True)


@router.post("/{slug}/clone", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def clone_app(
    slug: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """克隆已发布应用到当前用户名下：复制文件与元数据成为新项目。

    新项目不带发布记录（克隆与发布解耦），文件为物理拷贝，
    因此原项目与其公开链接独立演进，互不影响。
    """
    publication, source = _get_published(db, slug)
    project = Project(user_id=user.id, name=source.name, mode=source.mode)
    db.add(project)
    db.commit()
    db.refresh(project)

    source_root = project_dir(request, source.id)
    target_root = project_dir(request, project.id)
    try:
        # 只复制生成文件（白名单扩展名，自动排除快照等系统保留目录）。
        # 发布成功必有文件；空拷贝意味着源目录已消失（并发删除），不得静默交付空项目。
        files = iter_project_files(source_root)
        if not files:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="应用不存在或已下架"
            )
        target_root.mkdir(parents=True, exist_ok=True)
        for f in files:
            dest = target_root / f.relative_to(source_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        _sync_file_index(db, project.id, target_root)
        db.commit()
    except HTTPException:
        _abort_clone(db, project, target_root)
        raise
    except Exception:
        _abort_clone(db, project, target_root)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="克隆失败，请重试"
        )
    return project_payload(db, project)
