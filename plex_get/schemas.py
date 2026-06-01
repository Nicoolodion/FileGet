from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from .models import LinkStatus, MediaType, TaskStatus


class PasswordIn(BaseModel):
    value: str


class PasswordOut(BaseModel):
    id: int
    value: str
    position: int

    class Config:
        from_attributes = True


class LinkIn(BaseModel):
    original_url: str


class TaskCreate(BaseModel):
    media_type: MediaType = MediaType.UNCATEGORIZED
    title: Optional[str] = None
    raw_input: str


class LinkOut(BaseModel):
    id: int
    original_url: str
    debrided_url: str
    filename: str
    final_path: str
    status: LinkStatus
    progress: float
    speed: float
    error: str

    class Config:
        from_attributes = True


class TaskOut(BaseModel):
    id: int
    media_type: MediaType
    status: TaskStatus
    title: str
    raw_input: str
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime]
    log: str
    links: list[LinkOut] = []

    class Config:
        from_attributes = True
