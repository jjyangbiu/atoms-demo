"""版本快照：每次成功生成自动留档，可回滚恢复（工单 0007）。

文件本体以磁盘全量副本存放于 {storage_root}/projects/{id}/snapshots/{rev}/，
数据库只存索引（rev + 时间 + 文件数），与 ProjectFile「磁盘为准」同口径。
"""

import shutil
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .agent.tools import ALLOWED_EXTENSIONS, RESERVED_DIRS
from .models import Snapshot


def snapshots_root(project_root: Path) -> Path:
    """项目目录内的快照存放区（系统保留目录，智能体与预览不可触碰）。"""
    return project_root / "snapshots"


def iter_project_files(root: Path) -> list[Path]:
    """项目目录下当前全部生成文件（白名单扩展名，排除快照等保留目录）。"""
    if not root.is_dir():
        return []
    result = []
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        if f.relative_to(root).parts[0] in RESERVED_DIRS:
            continue
        result.append(f)
    return sorted(result)


def list_snapshot_files(project_root: Path, snapshot: Snapshot) -> list[tuple[str, int]]:
    """快照留档内的文件清单 (相对路径, 字节数)，按路径排序。"""
    source = snapshots_root(project_root) / str(snapshot.rev)
    if not source.is_dir():
        return []
    return [
        (f.relative_to(source).as_posix(), f.stat().st_size)
        for f in sorted(source.rglob("*"))
        if f.is_file()
    ]


def create_snapshot(
    session: Session, project_id: int, project_root: Path, max_kept: int
) -> Snapshot:
    """把磁盘当前文件留档为新快照并返回；超出保留上限时清理最旧版本。"""
    next_rev = (
        session.scalar(
            select(func.max(Snapshot.rev)).where(Snapshot.project_id == project_id)
        )
        or 0
    ) + 1
    target = snapshots_root(project_root) / str(next_rev)
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in iter_project_files(project_root):
        rel = f.relative_to(project_root)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        count += 1
    snapshot = Snapshot(project_id=project_id, rev=next_rev, file_count=count)
    session.add(snapshot)
    _prune_old_snapshots(session, project_id, project_root, max_kept)
    return snapshot


def restore_snapshot(project_root: Path, snapshot: Snapshot) -> None:
    """用快照留档整体替换项目目录当前文件（回滚）。

    回滚不产生新快照；后续成功迭代会在恢复后的基线上留档新版本。
    """
    source = snapshots_root(project_root) / str(snapshot.rev)
    for f in iter_project_files(project_root):
        f.unlink()
    # 清掉留下的空目录（保留快照存放区本身）
    reserved = snapshots_root(project_root)
    for d in sorted(project_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and d != reserved and reserved not in d.parents and not any(d.iterdir()):
            d.rmdir()
    if source.is_dir():
        for f in source.rglob("*"):
            if not f.is_file():
                continue
            dest = project_root / f.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)


def _prune_old_snapshots(
    session: Session, project_id: int, project_root: Path, max_kept: int
) -> None:
    """超过 max_kept 版时，从最旧起连同留档文件一并清理。"""
    if max_kept <= 0:
        return
    all_rows = session.scalars(
        select(Snapshot).where(Snapshot.project_id == project_id).order_by(Snapshot.rev.desc())
    ).all()
    for stale in all_rows[max_kept:]:
        shutil.rmtree(snapshots_root(project_root) / str(stale.rev), ignore_errors=True)
        session.delete(stale)
