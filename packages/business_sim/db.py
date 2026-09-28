"""数据库连接与会话工厂。默认 SQLite，生产改 DATABASE_URL 切 PostgreSQL。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "synthetic" / "business.db"


def default_database_url() -> str:
    return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or default_database_url()


def create_db_engine(url: str | None = None, **engine_kwargs: Any) -> Engine:
    """创建 Engine。SQLite 需放开线程检查，后续 FastAPI 会从多个线程访问同一文件库。"""
    resolved = url or database_url()
    if resolved.startswith("sqlite"):
        connect_args = engine_kwargs.setdefault("connect_args", {})
        connect_args.setdefault("check_same_thread", False)
    return create_engine(resolved, future=True, **engine_kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """建表。生产环境应改用迁移脚本；D1 仅用于合成库初始化。"""
    from .models import Base

    Base.metadata.create_all(engine)
