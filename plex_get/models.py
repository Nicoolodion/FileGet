from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class MediaType(str, Enum):
    MOVIE = "movie"
    SERIES = "series"
    ANIME_MOVIE = "anime_movie"
    ANIME_SERIES = "anime_series"
    UNCATEGORIZED = "uncategorized"


class LinkStatus(str, Enum):
    PENDING = "pending"
    DEBRIDDING = "debriding"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    MOVING = "moving"
    DONE = "done"
    FAILED = "failed"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Password(Base):
    __tablename__ = "passwords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[MediaType] = mapped_column(SAEnum(MediaType), default=MediaType.UNCATEGORIZED)
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus), default=TaskStatus.QUEUED)
    title: Mapped[str] = mapped_column(String(500), default="")
    raw_input: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log: Mapped[str] = mapped_column(Text, default="")

    links: Mapped[list["DownloadLink"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class DownloadLink(Base):
    __tablename__ = "download_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    original_url: Mapped[str] = mapped_column(Text)
    debrided_url: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(500), default="")
    expected_filename: Mapped[str] = mapped_column(String(500), default="")
    expected_size: Mapped[int] = mapped_column(Integer, default=0)
    final_path: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[LinkStatus] = mapped_column(SAEnum(LinkStatus), default=LinkStatus.PENDING)
    progress: Mapped[float] = mapped_column(default=0.0)
    speed: Mapped[float] = mapped_column(default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    task: Mapped[Task] = relationship(back_populates="links")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
