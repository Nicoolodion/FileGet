from __future__ import annotations

import pytest

from plex_get.models import DownloadLink, LinkStatus, MediaType, Task, TaskStatus
from plex_get.manager import Manager


def _make_task_with_links(temp_db, *, n: int, names: list[str], sizes: list[int], statuses: list[LinkStatus]) -> tuple[int, list[int]]:
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        t = Task(media_type=MediaType.MOVIE, status=TaskStatus.PROCESSING)
        db.add(t)
        db.commit()
        db.refresh(t)
        ids: list[int] = []
        for i in range(n):
            l = DownloadLink(
                task_id=t.id,
                original_url=f'https://example.com/{i}',
                expected_filename=names[i] if i < len(names) else '',
                expected_size=sizes[i] if i < len(sizes) else 0,
                status=statuses[i] if i < len(statuses) else LinkStatus.DOWNLOADING,
            )
            db.add(l)
            db.commit()
            db.refresh(l)
            ids.append(l.id)
        return t.id, ids


def test_set_membership_uses_expected_filename(temp_db) -> None:
    """A link that hasn't finished downloading yet should still be identified as a
    member of its multi-volume set via its expected_filename (recorded from the
    response headers)."""
    names = [
        'movie.part1.rar', 'movie.part2.rar', 'movie.part3.rar', 'movie.part4.rar',
    ]
    task_id, ids = _make_task_with_links(
        temp_db,
        n=4,
        names=names,
        sizes=[100] * 4,
        statuses=[LinkStatus.DOWNLOADING] * 4,
    )
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        links = list(db.query(DownloadLink).filter(DownloadLink.task_id == task_id))
    m = Manager()
    base, _, members = m._set_membership(links, ids[0])
    assert base == 'movie.rar'
    assert {l.id for l in members} == set(ids)


def test_set_membership_falls_back_to_filename(temp_db) -> None:
    """When expected_filename is missing (e.g. header not parseable), fall back to filename."""
    task_id, ids = _make_task_with_links(
        temp_db,
        n=2,
        names=['', ''],
        sizes=[0, 0],
        statuses=[LinkStatus.DONE, LinkStatus.DONE],
    )
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        for lid, fname in zip(ids, ['movie.part1.rar', 'movie.part2.rar']):
            link = db.get(DownloadLink, lid)
            link.filename = fname
            db.commit()
        links = list(db.query(DownloadLink).filter(DownloadLink.task_id == task_id))
    m = Manager()
    base, _, members = m._set_membership(links, ids[0])
    assert base == 'movie.rar'
    assert {l.id for l in members} == set(ids)


def test_set_membership_ignores_other_sets(temp_db) -> None:
    task_id, ids = _make_task_with_links(
        temp_db,
        n=4,
        names=['a.part1.rar', 'a.part2.rar', 'b.part1.rar', 'b.part2.rar'],
        sizes=[0, 0, 0, 0],
        statuses=[LinkStatus.DONE, LinkStatus.DONE, LinkStatus.DONE, LinkStatus.DONE],
    )
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        links = list(db.query(DownloadLink).filter(DownloadLink.task_id == task_id))
    m = Manager()
    base, _, members = m._set_membership(links, ids[0])
    assert base == 'a.rar'
    assert {l.id for l in members} == {ids[0], ids[1]}


def test_set_membership_standalone_rar(temp_db) -> None:
    """A RAR without .partN is NOT a multi-volume set - returns base=None."""
    task_id, ids = _make_task_with_links(
        temp_db,
        n=1,
        names=['movie.rar'],
        sizes=[0],
        statuses=[LinkStatus.DONE],
    )
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        links = list(db.query(DownloadLink).filter(DownloadLink.task_id == task_id))
    m = Manager()
    base, _, members = m._set_membership(links, ids[0])
    assert base is None
    assert members == []
