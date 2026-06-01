from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_db_path = Path(_settings.database_path)
_db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{_db_path}",
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # Lightweight migrations: add new columns to existing tables without Alembic.
    with engine.begin() as conn:
        from sqlalchemy import text
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(download_links)")).fetchall()}
        if 'expected_filename' not in cols:
            conn.execute(text("ALTER TABLE download_links ADD COLUMN expected_filename VARCHAR(500) DEFAULT ''"))
        if 'expected_size' not in cols:
            conn.execute(text("ALTER TABLE download_links ADD COLUMN expected_size INTEGER DEFAULT 0"))
