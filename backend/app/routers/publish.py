"""发布与稳定链接（工单 0006）：发布/取消发布 + 匿名公开托管。

- 发布为项目分配唯一稳定 slug，返回 /p/{slug} 公开链接
- 公开托管直接读取项目目录的当前文件：迭代成功后内容自动更新而链接不变
- 取消发布删除发布记录，公开链接立即 404
- 同一项目至多一条活跃发布记录（models.Publication.project_id 唯一 + 幂等接口）
"""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
from ..models import Project, Publication, User
from ..schemas import PublishOut
from ..serving import serve_project_file
from .projects import get_owned_project, project_dir

router = APIRouter(prefix="/api/projects", tags=["publish"])
public_router = APIRouter(tags=["public"])

# 去除易混淆字符（0/o/1/l/i）的小写字母数字表
_SLUG_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_SLUG_LENGTH = 8


def _generate_slug(db: Session) -> str:
    """生成一个未被占用的随机 slug。"""
    for _ in range(10):
        slug = "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(_SLUG_LENGTH))
        if db.scalar(select(Publication.id).where(Publication.slug == slug)) is None:
            return slug
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="slug 生成失败，请重试"
    )


def get_publication(db: Session, project_id: int) -> Publication | None:
    return db.scalar(select(Publication).where(Publication.project_id == project_id))


@router.post(
    "/{project_id}/publish", response_model=PublishOut, status_code=status.HTTP_201_CREATED
)
def publish_project(
    project_id: int,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PublishOut:
    get_owned_project(project_id, user, db)
    existing = get_publication(db, project_id)
    if existing is not None:
        # 幂等：重复发布返回同一稳定链接
        response.status_code = status.HTTP_200_OK
        return PublishOut(slug=existing.slug, url=f"/p/{existing.slug}")
    if not (project_dir(request, project_id) / "index.html").is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="还没有可发布的内容，请先生成应用"
        )
    slug = _generate_slug(db)
    db.add(Publication(project_id=project_id, slug=slug))
    db.commit()
    return PublishOut(slug=slug, url=f"/p/{slug}")


@router.delete("/{project_id}/publish", status_code=status.HTTP_204_NO_CONTENT)
def unpublish_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    get_owned_project(project_id, user, db)
    publication = get_publication(db, project_id)
    if publication is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目尚未发布")
    db.delete(publication)
    db.commit()


# --- 公开托管：/p/{slug} 任何人无需登录即可访问 ---
#
# 内容直接取自项目目录当前文件，因此发布后的每次迭代成功都会
# 自动同步到同一链接，无需任何额外发布动作。
#
# 安全：用户生成的脚本与主站同源运行会窃取登录者的令牌/ Cookie，
# CSP sandbox 令文档获得不透明源（无法触碰主站 localStorage 与 Cookie），
# 脚本仍可执行。彻底隔离需独立域名/端口托管（待部署工单落地）。
_PUBLIC_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals"


def _serve_public(request: Request, slug: str, rel_path: str, db: Session) -> Response:
    publication = db.scalar(select(Publication).where(Publication.slug == slug))
    if publication is None or db.get(Project, publication.project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="应用不存在或已下架")
    response = serve_project_file(project_dir(request, publication.project_id), rel_path)
    if (response.media_type or "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = _PUBLIC_CSP
    return response


@public_router.get("/p/{slug}")
def public_root(slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    return _serve_public(request, slug, "index.html", db)


@public_router.get("/p/{slug}/{file_path:path}")
def public_file(
    slug: str, file_path: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    return _serve_public(request, slug, file_path.strip("/") or "index.html", db)
