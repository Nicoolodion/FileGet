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
    find_main_video,
    find_rar_archive,
    group_rar_volumes,
    list_rar_volumes,
    safe_move,
    safe_rmtree,
)
from .models import DownloadLink, LinkStatus, MediaType, Password, Task, TaskStatus
from .naming import final_path_for, is_series_type, parse_series_name

log = logging.getLogger(__name__)


class Manager:
    def __init__(self) -> None:
        self._sem: Optional[asyncio.Semaphore] = None
        self._max_concurrent = get_settings().max_concurrent_downloads
        self._configure_semaphore(self._max_concurrent)
        self._stop = asyncio.Event()
        self._paused = asyncio.Event()
        self._paused.set()  # not paused initially
        self._worker_task: Optional[asyncio.Task] = None
        self._link_futures: dict[int, asyncio.Future] = {}
        self._futures_lock = asyncio.Lock()

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
        self._paused.set()  # release any waiters so the worker exits
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
        """Pause dispatching new link downloads. In-flight downloads are not cancelled."""
        self._paused.clear()
        bus.publish_sync('manager', {'paused': True})

    def resume(self) -> None:
        self._paused.set()
        bus.publish_sync('manager', {'paused': False})

    def is_paused(self) -> bool:
        return not self._paused.is_set()

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
            except Exception as e:  # pragma: no cover - dispatch loop
                log.exception('dispatch loop error: %s', e)
                await asyncio.sleep(2)

    async def _process_task(self, task_id: int) -> None:
        try:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if not task:
                    return
                link_ids = [l.id for l in task.links]
            futures = []
            for lid in link_ids:
                fut = asyncio.create_task(self._process_link(task_id, lid))
                self._link_futures[lid] = fut
                futures.append(fut)
            if futures:
                await asyncio.gather(*futures, return_exceptions=True)
            await self._finalize_task(task_id)
        except Exception as e:
            log.exception('_process_task error: %s', e)
            await self._set_task_status(task_id, TaskStatus.FAILED, str(e))

    async def _process_link(self, task_id: int, link_id: int) -> None:
        async with self._sem:
            try:
                debrided = await self._debrid_link(task_id, link_id)
                if not debrided:
                    return
                await self._download_link(task_id, link_id, debrided)
                await self._set_link_status(link_id, LinkStatus.EXTRACTING)
            except Exception as e:
                log.exception('link %s failed: %s', link_id, e)
                await self._set_link_status(link_id, LinkStatus.FAILED, error=str(e))
                await self._append_task_log(task_id, f'Link {link_id} failed: {e}')
            finally:
                async with self._futures_lock:
                    self._link_futures.pop(link_id, None)

    async def _debrid_link(self, task_id: int, link_id: int) -> Optional[str]:
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return None
            original = link.original_url
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

    async def _finalize_task(self, task_id: int) -> None:
        """After all links are downloaded, group multi-volume RARs and extract each set."""
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            links = list(task.links)
        any_failed = any(l.status == LinkStatus.FAILED for l in links)
        all_done = all(l.status in (LinkStatus.DONE, LinkStatus.FAILED) for l in links)
        if not all_done:
            return
        if any_failed and not all(l.status == LinkStatus.DONE for l in links):
            await self._set_task_status(task_id, TaskStatus.FAILED, 'One or more links failed')
            await self._cleanup_task_dir(task_id)
            return

        settings = get_settings()
        temp_root = Path(settings.temp_path)
        task_dir = temp_root / f'task_{task_id}'
        extract_root = task_dir / 'extracted'
        extract_root.mkdir(parents=True, exist_ok=True)

        rar_files = list_rar_volumes(task_dir)
        with SessionLocal() as db:
            passwords = [p.value for p in db.query(Password).order_by(Password.position.asc(), Password.id.asc()).all()]
        if rar_files:
            groups = group_rar_volumes(rar_files)
            await self._append_task_log(task_id, f'Found {len(groups)} archive set(s) to extract')
            for group in groups:
                first = group[0]
                # Extract the whole set at once; rarfile walks .part1..partN
                extract_dest = extract_root / first.stem
                extract_dest.mkdir(parents=True, exist_ok=True)
                note = f'Extracting {first.name}' + (f' (+{len(group)-1} part)' if len(group) > 1 else '') + f' (passwords: {len(passwords)})'
                await self._append_task_log(task_id, note)
                try:
                    extracted_video = extract_archive(first, extract_dest, passwords, first_volume=first)
                except Exception as e:
                    raise RuntimeError(f'Extraction failed for {first.name} (all passwords tried): {e}') from e
                if not extracted_video or not extracted_video.exists():
                    raise RuntimeError(f'Could not locate a video file after extracting {first.name}')
                await self._move_video(task_id, first.name, extracted_video)
        else:
            # No archives - move any video files at the task root directly
            for f in task_dir.iterdir():
                if f.is_file() and f.suffix.lower() in {'.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts', '.webm'}:
                    await self._move_video(task_id, f.name, f)

        with SessionLocal() as db:
            for link in links:
                link.status = LinkStatus.DONE
                db.add(link)
            db.commit()
        await self._set_task_status(task_id, TaskStatus.COMPLETED)
        await self._cleanup_task_dir(task_id)

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
        import shutil as _sh
        return _sh.disk_usage(str(path)).free
    except Exception:
        return None


_manager: Optional[Manager] = None


def get_manager() -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager()
    return _manager
