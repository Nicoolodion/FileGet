from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from plex_get.extractor import volume_group_key
from plex_get.manager import Manager


def test_volume_group_key_part() -> None:
    assert volume_group_key('foo.part1.rar') == ('foo.rar', 1)
    assert volume_group_key('foo.part10.rar') == ('foo.rar', 10)
    assert volume_group_key('foo.part1.RAR') == ('foo.rar', 1)
    assert volume_group_key('foo.rar') == ('foo.rar', 0)
    assert volume_group_key('foo.txt') == ('foo.txt', 0)


def test_manager_starts_unpaused() -> None:
    assert Manager().is_paused() is False


def test_manager_pause_and_resume() -> None:
    m = Manager()
    m.pause()
    assert m.is_paused() is True
    m.resume()
    assert m.is_paused() is False


def test_manager_pause_releases_dispatch_waiter() -> None:
    async def runner() -> bool:
        m = Manager()
        m.pause()
        m.resume()
        await asyncio.wait_for(m._paused.wait(), timeout=0.5)
        return True

    assert asyncio.run(runner()) is True


def test_free_bytes_known_path() -> None:
    from plex_get.manager import _free_bytes
    n = _free_bytes(Path('.'))
    assert n is None or n > 0


@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Deprecated: use conftest.temp_db instead. Kept for backwards compat."""
    from plex_get import db as dbmod
    from plex_get import manager as mgrmod
    db_file = tmp_path / 'plex-get.db'
    monkeypatch.setenv('DATABASE_PATH', str(db_file))
    dbmod.get_settings.cache_clear()
    new_engine = dbmod.create_engine(f'sqlite:///{db_file}', connect_args={'check_same_thread': False}, future=True)
    new_session = dbmod.sessionmaker(bind=new_engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(dbmod, 'engine', new_engine)
    monkeypatch.setattr(dbmod, 'SessionLocal', new_session)
    monkeypatch.setattr(mgrmod, 'SessionLocal', new_session)
    from plex_get.db import init_db
    init_db()
    yield dbmod


def test_cancel_link_marks_failed(temp_db) -> None:
    from plex_get.db import SessionLocal
    from plex_get.models import DownloadLink, LinkStatus, MediaType, Task, TaskStatus

    with SessionLocal() as db:
        t = Task(media_type=MediaType.MOVIE, status=TaskStatus.PROCESSING)
        db.add(t)
        db.commit()
        db.refresh(t)
        l = DownloadLink(task_id=t.id, original_url='https://example.com/x', status=LinkStatus.DOWNLOADING)
        db.add(l)
        db.commit()
        db.refresh(l)
        link_id = l.id

    m = Manager()
    asyncio.run(m.cancel_link(link_id))
    with SessionLocal() as db:
        l = db.get(DownloadLink, link_id)
        assert l.status == LinkStatus.FAILED
        assert 'cancel' in l.error.lower()


def test_retry_link_resets_state(temp_db) -> None:
    from plex_get.db import SessionLocal
    from plex_get.models import DownloadLink, LinkStatus, MediaType, Task, TaskStatus

    with SessionLocal() as db:
        t = Task(media_type=MediaType.MOVIE, status=TaskStatus.PROCESSING)
        db.add(t)
        db.commit()
        db.refresh(t)
        l = DownloadLink(task_id=t.id, original_url='https://example.com/x', status=LinkStatus.FAILED, error='boom', progress=0.5, speed=100, debrided_url='x', filename='x.rar')
        db.add(l)
        db.commit()
        db.refresh(l)
        link_id = l.id

    m = Manager()
    asyncio.run(m.reset_link_state(link_id))
    with SessionLocal() as db:
        l = db.get(DownloadLink, link_id)
        assert l.status == LinkStatus.PENDING
        assert l.error == ''
        assert l.progress == 0.0
        assert l.speed == 0.0
        assert l.debrided_url == ''
        assert l.filename == ''


