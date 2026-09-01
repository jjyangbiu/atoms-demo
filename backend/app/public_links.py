"""公开链接的 nginx 直出维护（工单 0013）。

容器化部署中 /p/{slug} 静态由 nginx 直出、不经后端：
后端在 {storage_root}/p/ 下维护符号链接 {slug} → {storage_root}/projects/{project_id}，
nginx 的 root 指向 storage_root 即可按 URI 直出（见 frontend/nginx.conf）。

后端 /p/ 路由（routers/publish.py）保留为直连兜底，两者同一数据源：
符号链接无法建立时（文件系统不支持等）仅"直出"退化为"经后端"，
发布/下架的业务行为不受影响，因此所有链接操作失败都静默降级。
"""

import logging
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .models import Publication

logger = logging.getLogger(__name__)


def links_root(storage_root: str | Path) -> Path:
    """slug 符号链接所在目录（与 projects/ 平级，nginx 按 /p/ 前缀直出）。"""
    return Path(storage_root) / "p"


def _link_target(storage_root: str | Path, project_id: int) -> str:
    # 绝对目标：Windows 不支持可用的相对符号链接；容器化时 backend 与 nginx
    # 把同一数据卷挂到同一路径（见 docker-compose.yml），链接两边均有效，
    # 即便挂载路径变化，启动 resync 也会按当前路径重建。
    return str((Path(storage_root) / "projects" / str(project_id)).resolve())


def ensure_link(storage_root: str | Path, slug: str, project_id: int) -> None:
    """建立 slug → 项目目录的符号链接（幂等）；失败静默降级。"""
    link = links_root(storage_root) / slug
    target = _link_target(storage_root, project_id)
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if os.readlink(link) == target:
                return
            link.unlink()
        elif link.exists():
            # 非链接的同名真实目录：不擅自替换，仅记录异常
            logger.warning("公开链接 %s 被非链接路径占用，跳过", slug)
            return
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        logger.warning("建立公开链接 %s 失败，nginx 直出将退化为后端兜底", slug)


def remove_link(storage_root: str | Path, slug: str) -> None:
    """移除 slug 符号链接（只删链接本身，绝不触碰项目目录）；失败静默降级。"""
    link = links_root(storage_root) / slug
    try:
        if link.is_symlink():
            link.unlink()
    except OSError:
        logger.warning("移除公开链接 %s 失败", slug)


def resync(engine: Engine, storage_root: str | Path) -> None:
    """按发布记录重建链接目录（启动时调用，覆盖容器重启后的恢复）。

    缺失的补齐、孤立的清除（对应发布已不存在）；逐条降级，不影响启动。
    """
    desired: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            for slug, project_id in conn.execute(
                select(Publication.slug, Publication.project_id)
            ):
                desired[slug] = project_id
    except Exception:  # noqa: BLE001 — 链接重建失败不得阻断启动
        logger.warning("读取发布记录失败，跳过公开链接重建")
        return

    root = links_root(storage_root)
    try:
        if root.is_dir():
            for entry in root.iterdir():
                if entry.name not in desired and entry.is_symlink():
                    entry.unlink()
    except OSError:
        logger.warning("清理孤立公开链接失败")

    for slug, project_id in desired.items():
        ensure_link(storage_root, slug, project_id)
