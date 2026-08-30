"""数据库接入：SQLAlchemy 引擎与声明式基类。"""

from pathlib import Path

from sqlalchemy import create_engine
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
