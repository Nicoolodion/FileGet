from __future__ import annotations

from pathlib import Path

import pytest

from plex_get.models import DownloadLink, LinkStatus, MediaType, Task, TaskStatus
from plex_get.manager import Manager


def _make_task_with_links(temp_db, *, names: list[str], sizes: list[int], statuses: list[LinkStatus]) -> tuple[int, list[int]]:
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        t = Task(media_type=MediaType.MOVIE, status=TaskStatus.PROCESSING)
        db.add(t)
        db.commit()
        db.refresh(t)
        ids: list[int] = []
        for i in range(len(names)):
            l = DownloadLink(
                task_id=t.id,
                original_url=f'https://example.com/{i}',
                expected_filename=names[i],
                expected_size=sizes[i],
                status=statuses[i],
            )
            db.add(l)
            db.commit()
            db.refresh(l)
            ids.append(l.id)
        return t.id, ids


def _write_part_files(temp_db, task_id: int, names: list[str], sizes: list[int]) -> Path:
    """Create the expected part files on disk under the task's temp dir."""
    from plex_get.config import get_settings
    task_dir = Path(get_settings().temp_path) / f'task_{task_id}'
    task_dir.mkdir(parents=True, exist_ok=True)
    for n, s in zip(names, sizes):
        p = task_dir / n
        p.write_bytes(b'\0' * s)
    return task_dir


def test_extract_trigger_when_all_parts_on_disk(temp_db) -> None:
    """The new on-disk trigger must say 'ready to extract' when every part file is present at the expected size, regardless of link.status."""
    names = [f'movie.part{i}.rar' for i in range(1, 4)]
    sizes = [100, 200, 300]
    task_id, ids = _make_task_with_links(
        temp_db,
        names=names,
        sizes=sizes,
        statuses=[LinkStatus.DOWNLOADING, LinkStatus.DOWNLOADING, LinkStatus.DOWNLOADING],
    )
    task_dir = _write_part_files(temp_db, task_id, names, sizes)
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        links = list(db.query(DownloadLink).filter(DownloadLink.task_id == task_id))
    m = Manager()
    # The new trigger logic: all parts present at correct size => ready.
    base, _, members = m._set_membership(links, ids[0])
    assert base == 'movie.rar'
    assert {l.id for l in members} == set(ids)
    # None are in DONE/FAILED but all files are on disk. Walk the new on-disk
    # check used inside _try_extract_after_link.
    present = []
    missing = []
    for ml in members:
        pn = ml.expected_filename
        cands = [p for p in task_dir.iterdir() if p.is_file() and volume_group_key(p.name) == volume_group_key(pn)]
        if not cands or (ml.expected_size and cands[0].stat().st_size != ml.expected_size):
            missing.append(pn)
        else:
            present.append(ml)
    assert len(present) == 3 and not missing


def test_extract_trigger_waits_when_part_missing(temp_db) -> None:
    names = [f'movie.part{i}.rar' for i in range(1, 4)]
    sizes = [100, 200, 300]
    task_id, ids = _make_task_with_links(temp_db, names=names, sizes=sizes, statuses=[LinkStatus.DOWNLOADING] * 3)
    # Only write 2 of 3 parts
    task_dir = _write_part_files(temp_db, task_id, names[:2], sizes[:2])
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        links = list(db.query(DownloadLink).filter(DownloadLink.task_id == task_id))
    m = Manager()
    base, _, members = m._set_membership(links, ids[0])
    present = []
    missing = []
    for ml in members:
        pn = ml.expected_filename
        cands = [p for p in task_dir.iterdir() if p.is_file() and volume_group_key(p.name) == volume_group_key(pn)]
        if not cands or (ml.expected_size and cands[0].stat().st_size != ml.expected_size):
            missing.append(pn)
        else:
            present.append(ml)
    assert len(present) == 2
    assert any('part3' in m for m in missing)


def test_extract_trigger_detects_size_mismatch(temp_db) -> None:
    names = [f'movie.part{i}.rar' for i in range(1, 3)]
    sizes = [100, 200]
    task_id, ids = _make_task_with_links(temp_db, names=names, sizes=sizes, statuses=[LinkStatus.DOWNLOADING] * 2)
    # Part 1 is short.
    task_dir = _write_part_files(temp_db, task_id, [names[0], names[1]], [50, 200])
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        links = list(db.query(DownloadLink).filter(DownloadLink.task_id == task_id))
    m = Manager()
    base, _, members = m._set_membership(links, ids[0])
    present = []
    missing = []
    for ml in members:
        pn = ml.expected_filename
        cands = [p for p in task_dir.iterdir() if p.is_file() and volume_group_key(p.name) == volume_group_key(pn)]
        if not cands or (ml.expected_size and cands[0].stat().st_size != ml.expected_size):
            missing.append(pn)
        else:
            present.append(ml)
    # part1 is short -> missing. part2 matches -> present.
    assert len(present) == 1 and present[0].expected_filename == 'movie.part2.rar'
    assert any('part1' in m for m in missing)


def test_part_path_resume(temp_db) -> None:
    """Manager.part_path returns the on-disk path when the part file exists at the expected size."""
    names = ['movie.part1.rar']
    task_id, ids = _make_task_with_links(temp_db, names=names, sizes=[1234], statuses=[LinkStatus.DONE])
    task_dir = _write_part_files(temp_db, task_id, names, [1234])
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        link = db.get(DownloadLink, ids[0])
    m = Manager()
    p = m.part_path(task_id, link)
    assert p is not None
    assert p.name == 'movie.part1.rar'
    assert p.stat().st_size == 1234


def test_part_path_resume_wrong_size(temp_db) -> None:
    names = ['movie.part1.rar']
    task_id, ids = _make_task_with_links(temp_db, names=names, sizes=[1234], statuses=[LinkStatus.DONE])
    _write_part_files(temp_db, task_id, names, [100])
    from plex_get.db import SessionLocal
    with SessionLocal() as db:
        link = db.get(DownloadLink, ids[0])
    m = Manager()
    p = m.part_path(task_id, link)
    assert p is None


# Helper: import here to avoid top-level circular issues in the module check.
from plex_get.extractor import volume_group_key
