"""数据库接入：SQLAlchemy 引擎、声明式基类与存量库补列。"""

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str):
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.get_backend_name() == "sqlite" else {}
    return create_engine(url, connect_args=connect_args)


def ensure_schema(engine) -> None:
    """create_all 不改存量表：首期后新增的列对旧库自动补齐。

    项目没有迁移框架且只支持 SQLite（同 official_samples._ensure_official_column 先例）：
    工单 0018 给 tickets 表新增检查点快照引用列 snapshot_id。
    """
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tickets)"))}
    if columns and "snapshot_id" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN snapshot_id INTEGER"))
