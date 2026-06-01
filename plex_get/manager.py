from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .debrid import DebridError, get_client
from .db import SessionLocal
from .events import bus
from .extractor import (
    configure_rarfile,
    extract_archive,
    find_rar_archive,
    find_main_video,
    group_rar_volumes,
    list_rar_volumes,
    safe_move,
    safe_rmtree,
    volume_group_key,
)
from .models import DownloadLink, LinkStatus, MediaType, Password, Task, TaskStatus
from .naming import final_path_for, is_series_type, parse_series_name
from .notifier import notify_task

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {'.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts', '.webm'}


class Manager:
    def __init__(self) -> None:
        self._sem: Optional[asyncio.Semaphore] = None
        self._max_concurrent = get_settings().max_concurrent_downloads
        self._configure_semaphore(self._max_concurrent)
        self._stop = asyncio.Event()
        self._paused = asyncio.Event()
        self._paused.set()
        self._worker_task: Optional[asyncio.Task] = None
        self._link_futures: dict[int, asyncio.Future] = {}
        self._futures_lock = asyncio.Lock()
        self._cancelled_links: set[int] = set()
        self._cancelled_lock = asyncio.Lock()

    def _configure_semaphore(self, n: int) -> None:
        self._sem = asyncio.Semaphore(max(1, n))

    async def start(self) -> None:
        configure_rarfile()
        if self._worker_task is None or self._worker_task.done():
            self._stop.clear()
            self._paused.set()
            self._worker_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        self._stop.set()
        self._paused.set()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass

    def set_concurrency(self, n: int) -> None:
        self._max_concurrent = max(1, n)
        self._configure_semaphore(self._max_concurrent)

    def pause(self) -> None:
        self._paused.clear()
        bus.publish_sync('manager', {'paused': True})

    def resume(self) -> None:
        self._paused.set()
        bus.publish_sync('manager', {'paused': False})

    def is_paused(self) -> bool:
        return not self._paused.is_set()

    async def cancel_link(self, link_id: int) -> None:
        async with self._cancelled_lock:
            self._cancelled_links.add(link_id)
        fut = self._link_futures.get(link_id)
        if fut and not fut.done():
            fut.cancel()
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if link and link.status not in (LinkStatus.DONE, LinkStatus.FAILED):
                link.status = LinkStatus.FAILED
                link.error = 'cancelled'
                db.commit()
        await bus.publish('links', {'id': link_id, 'status': 'failed', 'error': 'cancelled'})

    async def reset_link_state(self, link_id: int) -> Optional[int]:
        """Reset a failed link to PENDING. Returns the link's task_id (or None)."""
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return None
            task_id = link.task_id
            link.status = LinkStatus.PENDING
            link.error = ''
            link.progress = 0.0
            link.speed = 0.0
            link.debrided_url = ''
            link.filename = ''
            db.commit()
        await bus.publish('links', {'id': link_id, 'status': 'pending', 'error': '', 'progress': 0, 'speed': 0})
        return task_id

    async def retry_link(self, link_id: int) -> None:
        task_id = await self.reset_link_state(link_id)
        if task_id is None:
            return
        await self._append_task_log(task_id, f'Retrying link {link_id}')
        asyncio.create_task(self._process_link(task_id, link_id))

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._paused.wait()
                with SessionLocal() as db:
                    candidates = (
                        db.query(Task)
                        .filter(Task.status == TaskStatus.QUEUED)
                        .order_by(Task.created_at.asc())
                        .all()
                    )
                    if not candidates:
                        await asyncio.sleep(2)
                        continue
                    task = candidates[0]
                    task.status = TaskStatus.PROCESSING
                    db.commit()
                    db.refresh(task)
                    task_id = task.id
                asyncio.create_task(self._process_task(task_id))
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover
                log.exception('dispatch loop error: %s', e)
                await asyncio.sleep(2)

    async def _process_task(self, task_id: int) -> None:
        """Fan out per-link workers, then wait for all of them to finish.

        Each worker is responsible for downloading a single link and signalling
        when it has finished so that per-volume-set extraction can be triggered.
        The task itself is finalized after every link has reached a terminal
        state (DONE/FAILED)."""
        try:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if not task:
                    return
                link_ids = [l.id for l in task.links]
            if not link_ids:
                await self._finalize_task(task_id)
                return
            futures = []
            for lid in link_ids:
                fut = asyncio.create_task(self._process_link(task_id, lid))
                self._link_futures[lid] = fut
                futures.append(fut)
            await asyncio.gather(*futures, return_exceptions=True)
            await self._finalize_task(task_id)
        except Exception as e:
            log.exception('_process_task error: %s', e)
            await self._set_task_status(task_id, TaskStatus.FAILED, str(e))

    async def _process_link(self, task_id: int, link_id: int) -> None:
        async with self._sem:
            if await self._is_cancelled(link_id):
                return
            try:
                debrided = await self._debrid_link(task_id, link_id)
                if not debrided:
                    return
                if await self._is_cancelled(link_id):
                    return
                path = await self._download_link(task_id, link_id, debrided)
                if await self._is_cancelled(link_id):
                    return
                await self._try_extract_after_link(task_id, link_id, path)
            except asyncio.CancelledError:
                async with self._cancelled_lock:
                    self._cancelled_links.discard(link_id)
                await self._append_task_log(task_id, f'Link {link_id} cancelled')
            except Exception as e:
                log.exception('link %s failed: %s', link_id, e)
                await self._set_link_status(link_id, LinkStatus.FAILED, error=str(e))
                await self._append_task_log(task_id, f'Link {link_id} failed: {e}')
            finally:
                async with self._futures_lock:
                    self._link_futures.pop(link_id, None)

    async def _is_cancelled(self, link_id: int) -> bool:
        async with self._cancelled_lock:
            return link_id in self._cancelled_links

    async def _debrid_link(self, task_id: int, link_id: int) -> Optional[str]:
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return None
            original = link.original_url
            existing = link.debrided_url
        if existing:
            await self._set_link_status(link_id, LinkStatus.DOWNLOADING)
            return existing
        await self._set_link_status(link_id, LinkStatus.DEBRIDDING)
        await self._append_task_log(task_id, f'Debriding: {original}')
        client = get_client()
        try:
            debrided = await client.get_debrid_link(original)
        except DebridError as e:
            await self._set_link_status(link_id, LinkStatus.FAILED, error=str(e))
            await self._append_task_log(task_id, f'Debrid failed: {e}')
            return None
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            link.debrided_url = debrided
            db.commit()
        await bus.publish('links', {'id': link_id, 'task_id': task_id, 'debrided_url': debrided, 'status': 'debrid_ok'})
        return debrided

    async def _download_link(self, task_id: int, link_id: int, url: str) -> Path:
        settings = get_settings()
        temp_root = Path(settings.temp_path)
        temp_root.mkdir(parents=True, exist_ok=True)
        task_dir = temp_root / f'task_{task_id}'
        task_dir.mkdir(parents=True, exist_ok=True)

        await self._set_link_status(link_id, LinkStatus.DOWNLOADING)
        await self._append_task_log(task_id, f'Downloading link {link_id}')

        timeout = httpx.Timeout(connect=30, read=None, write=30, pool=30)
        target: Optional[Path] = None
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream('GET', url) as resp:
                resp.raise_for_status()
                filename = _filename_from_response(resp) or f'download_{link_id}.bin'
                filename = _sanitize_filename(filename)
                target = task_dir / filename
                total = int(resp.headers.get('content-length', '0') or 0)
                if total:
                    free = _free_bytes(temp_root)
                    if free is not None and total > free:
                        raise OSError(
                            f'Not enough free space on temp volume: need {total} bytes, have {free} bytes '
                            f'(temp={temp_root})'
                        )
                # Record the expected filename + size on the link BEFORE writing
                # any bytes. This lets the manager identify multi-volume sets
                # even when sibling downloads are still in flight.
                with SessionLocal() as db:
                    link = db.get(DownloadLink, link_id)
                    if link is not None:
                        link.expected_filename = filename
                        link.expected_size = total
                        db.commit()
                received = 0
                start = time.time()
                last_update = 0.0
                with target.open('wb') as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
                        received += len(chunk)
                        now = time.time()
                        if now - last_update > 0.5:
                            elapsed = now - start
                            speed = received / elapsed if elapsed > 0 else 0.0
                            progress = (received / total) if total else 0.0
                            await self._update_link_progress(link_id, progress, speed)
                            last_update = now
                await self._update_link_progress(link_id, 1.0, 0.0)
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            link.filename = target.name
            db.commit()
        await self._append_task_log(task_id, f'Download finished: {target.name}')
        return target

    def _set_membership(self, links: list, link_id: int) -> tuple[str | None, int, list]:
        """Return (base_name, part_index, set_members) for the link's RAR set.

        Set membership is determined from `expected_filename` (or, as a
        fallback, `filename`), so links that are still downloading are correctly
        identified as part of the set even before they write any bytes.
        Returns (None, 0, []) for non-RAR files or single-volume archives.
        """
        link = next((l for l in links if l.id == link_id), None)
        if not link:
            return None, 0, []
        name = link.expected_filename or link.filename
        if not name or not name.lower().endswith('.rar'):
            return None, 0, []
        base, idx = volume_group_key(name)
        if idx == 0:
            return None, 0, []
        members = []
        for l in links:
            other_name = l.expected_filename or l.filename
            if not other_name or not other_name.lower().endswith('.rar'):
                continue
            other_base, _ = volume_group_key(other_name)
            if other_base == base:
                members.append(l)
        return base, idx, members

    async def _try_extract_after_link(self, task_id: int, link_id: int, downloaded: Path) -> None:
        """Inspect the link's file; if it is part of a multi-volume set, try to extract
        that set as soon as all its siblings have also finished downloading."""
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            links = list(task.links)
            passwords = [p.value for p in db.query(Password).order_by(Password.position.asc(), Password.id.asc()).all()]
            media_type: MediaType = task.media_type

        base, _, members = self._set_membership(links, link_id)
        if base is None:
            # Standalone RAR or non-archive: extract/move immediately.
            await self._set_link_status(link_id, LinkStatus.EXTRACTING)
            try:
                await self._extract_or_move_one(task_id, media_type, downloaded, passwords)
            except Exception as e:
                await self._append_task_log(task_id, f'Link {link_id} extraction failed: {e}')
                raise
            await self._set_link_status(link_id, LinkStatus.DONE)
            await bus.publish('links', {'id': link_id, 'task_id': task_id, 'status': 'done'})
            return

        # Multi-volume set: collect member states.
        not_terminal = [l for l in members if l.status not in (LinkStatus.DONE, LinkStatus.FAILED)]
        failed_members = [l for l in members if l.status == LinkStatus.FAILED]
        if not_terminal:
            # Wait for the rest to finish. Mark our own status as DOWNLOADING
            # still so the UI shows progress correctly.
            return
        if failed_members:
            # The set is broken. Mark all members FAILED (other than the
            # already-failed ones) and clean up any partial files on disk.
            ids_to_fail = [l.id for l in members if l.status != LinkStatus.FAILED]
            await self._fail_set_members(task_id, base, ids_to_fail, 'set has failed members')
            return

        # All members downloaded successfully. Verify every expected part is
        # on disk with the right size before letting rarfile touch anything.
        settings = get_settings()
        task_dir = Path(settings.temp_path) / f'task_{task_id}'
        missing = []
        for m in members:
            part_name = m.expected_filename or m.filename
            part_idx = volume_group_key(part_name)[1] if part_name else 0
            # Locate the actual file on disk for this part.
            candidates = [p for p in task_dir.iterdir() if p.is_file() and volume_group_key(p.name) == (volume_group_key(part_name)[0], part_idx)]
            if not candidates:
                missing.append(f'{part_name} (not on disk)')
                continue
            actual = candidates[0]
            if m.expected_size and actual.stat().st_size != m.expected_size:
                missing.append(f'{actual.name} (size {actual.stat().st_size} != expected {m.expected_size})')
        if missing:
            await self._fail_set_members(task_id, base, [l.id for l in members], 'incomplete parts on disk: ' + ', '.join(missing))
            return

        # Mark this link EXTRACTING (the one that triggered extraction) and run.
        await self._set_link_status(link_id, LinkStatus.EXTRACTING)
        try:
            await self._extract_volume_set(task_id, media_type, base, passwords)
        except Exception as e:
            await self._append_task_log(task_id, f'Set {base} extraction failed: {e}')
            await self._fail_set_members(task_id, base, [l.id for l in members if l.id != link_id and l.status != LinkStatus.FAILED], str(e))
            raise
        # Mark all members DONE (this one already via _set_link_status above).
        for m in members:
            if m.id != link_id:
                await self._set_link_status(m.id, LinkStatus.DONE)
                await bus.publish('links', {'id': m.id, 'task_id': task_id, 'status': 'done'})
        await bus.publish('links', {'id': link_id, 'task_id': task_id, 'status': 'done'})

    async def _fail_set_members(self, task_id: int, base: str, link_ids: list[int], reason: str) -> None:
        """Mark a set's remaining members as FAILED and delete their partial files on disk."""
        for lid in link_ids:
            with SessionLocal() as db:
                link = db.get(DownloadLink, lid)
                if not link or link.status == LinkStatus.FAILED:
                    continue
                link.status = LinkStatus.FAILED
                link.error = f'set {base}: {reason}'
                db.commit()
            await bus.publish('links', {'id': lid, 'status': 'failed', 'error': link.error})
        # Try to clean up on-disk parts (best-effort).
        try:
            settings = get_settings()
            task_dir = Path(settings.temp_path) / f'task_{task_id}'
            for p in list(task_dir.iterdir()):
                if p.is_file() and volume_group_key(p.name)[0] == base:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except Exception:
            pass
        await self._append_task_log(task_id, f'Set {base} failed: {reason}')

    async def _extract_volume_set(self, task_id: int, media_type: MediaType, base: str, passwords: list[str]) -> None:
        settings = get_settings()
        task_dir = Path(settings.temp_path) / f'task_{task_id}'
        files = [p for p in task_dir.iterdir() if p.is_file() and volume_group_key(p.name)[0] == base]
        if not files:
            raise RuntimeError(f'No files found on disk for set {base}')
        files.sort(key=lambda p: volume_group_key(p.name)[1] or 0)
        first = files[0]
        extract_dest = task_dir / 'extracted' / first.stem
        extract_dest.mkdir(parents=True, exist_ok=True)
        note = f'Extracting {first.name}' + (f' (+{len(files) - 1} parts)' if len(files) > 1 else '') + f' (passwords: {len(passwords)})'
        await self._append_task_log(task_id, note)
        extracted_video = extract_archive(first, extract_dest, passwords, first_volume=first)
        await self._move_video(task_id, first.name, extracted_video)
        # Clean up the source parts now that they're on the media drive.
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass

    async def _extract_or_move_one(self, task_id: int, media_type: MediaType, file: Path, passwords: list[str]) -> None:
        if file.suffix.lower() == '.rar':
            settings = get_settings()
            task_dir = Path(settings.temp_path) / f'task_{task_id}'
            extract_dest = task_dir / 'extracted' / file.stem
            extract_dest.mkdir(parents=True, exist_ok=True)
            await self._append_task_log(task_id, f'Extracting {file.name} (passwords: {len(passwords)})')
            video = extract_archive(file, extract_dest, passwords, first_volume=file)
            await self._move_video(task_id, file.name, video)
        elif file.suffix.lower() in VIDEO_SUFFIXES:
            await self._move_video(task_id, file.name, file)
        else:
            await self._append_task_log(task_id, f'Link file {file.name} is not a recognized media archive; leaving in place at {file}')
        try:
            file.unlink()
        except OSError:
            pass

    async def _move_video(self, task_id: int, source_filename: str, video_path: Path) -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            media_type: MediaType = task.media_type
            parsed = parse_series_name(source_filename)
            final = final_path_for(media_type, parsed, video_path.name)
        await self._append_task_log(task_id, f'Moving to {final}')
        final.parent.mkdir(parents=True, exist_ok=True)
        safe_move(video_path, final)
        await self._append_task_log(task_id, f'Done: {final}')

    async def _finalize_task(self, task_id: int) -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            links = list(task.links)
        if not links:
            await self._set_task_status(task_id, TaskStatus.COMPLETED)
            await notify_task(task_id, 'completed', 'Empty task')
            await self._cleanup_task_dir(task_id)
            return
        all_terminal = all(l.status in (LinkStatus.DONE, LinkStatus.FAILED) for l in links)
        if not all_terminal:
            return
        any_failed = any(l.status == LinkStatus.FAILED for l in links)
        all_done = all(l.status == LinkStatus.DONE for l in links)
        if any_failed and not all_done:
            await self._set_task_status(task_id, TaskStatus.FAILED, 'One or more links failed')
            await notify_task(task_id, 'failed', 'One or more links failed')
        elif all_done:
            await self._set_task_status(task_id, TaskStatus.COMPLETED)
            await notify_task(task_id, 'completed', f'All {len(links)} links done')
        else:
            await self._set_task_status(task_id, TaskStatus.COMPLETED)
        await self._cleanup_task_dir(task_id)

    async def _cleanup_task_dir(self, task_id: int) -> None:
        settings = get_settings()
        task_dir = Path(settings.temp_path) / f'task_{task_id}'
        safe_rmtree(task_dir)

    async def _set_link_status(self, link_id: int, status: LinkStatus, error: str = '') -> None:
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return
            link.status = status
            if error:
                link.error = error
            db.commit()
        await bus.publish('links', {'id': link_id, 'status': status.value, 'error': error})

    async def _update_link_progress(self, link_id: int, progress: float, speed: float) -> None:
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return
            link.progress = progress
            link.speed = speed
            db.commit()
        await bus.publish('links', {'id': link_id, 'progress': progress, 'speed': speed})

    async def _set_task_status(self, task_id: int, status: TaskStatus, error: str = '') -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            task.status = status
            if error:
                task.log = (task.log or '') + f'\n[error] {error}'
            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.finished_at = datetime.now(timezone.utc)
            db.commit()
        await bus.publish('tasks', {'id': task_id, 'status': status.value, 'error': error})

    async def _append_task_log(self, task_id: int, line: str) -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
            task.log = (task.log or '') + f'\n[{ts}] {line}'
            db.commit()
        await bus.publish('tasks', {'id': task_id, 'log': line})


def _filename_from_response(resp: httpx.Response) -> Optional[str]:
    cd = resp.headers.get('content-disposition', '')
    if 'filename=' in cd:
        try:
            part = cd.split('filename=', 1)[1]
            part = part.strip().strip('"').strip("'")
            if part:
                return _sanitize_filename(part)
        except Exception:
            pass
    url_path = str(resp.request.url).split('?', 1)[0]
    if '/' in url_path:
        name = url_path.rsplit('/', 1)[-1]
        if name and '' != name:
            return _sanitize_filename(name)
    return None


def _sanitize_filename(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    for c in bad:
        name = name.replace(c, '_')
    return name


def _free_bytes(path: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(str(path)).free
    except Exception:
        return None


_manager: Optional[Manager] = None


def get_manager() -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager()
    return _manager
