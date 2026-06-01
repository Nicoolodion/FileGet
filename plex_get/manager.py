from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .debrid import DebridError, get_client
from .db import SessionLocal
from .events import bus
from .extractor import (
    configure_rarfile,
    find_main_video,
    find_rar_archive,
    safe_move,
    safe_rmtree,
)
from .models import DownloadLink, LinkStatus, MediaType, Password, Task, TaskStatus
from .naming import final_path_for, is_series_type, parse_series_name

log = logging.getLogger(__name__)


class Manager:
    def __init__(self) -> None:
        self._sem: asyncio.Semaphore | None = None
        self._max_concurrent = get_settings().max_concurrent_downloads
        self._configure_semaphore(self._max_concurrent)
        self._stop = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._link_futures: dict[int, asyncio.Future] = {}
        self._futures_lock = asyncio.Lock()

    def _configure_semaphore(self, n: int) -> None:
        self._sem = asyncio.Semaphore(max(1, n))

    async def start(self) -> None:
        configure_rarfile()
        if self._worker_task is None or self._worker_task.done():
            self._stop.clear()
            self._worker_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._worker_task:
            await asyncio.wait([self._worker_task], timeout=5)

    def set_concurrency(self, n: int) -> None:
        self._max_concurrent = max(1, n)
        self._configure_semaphore(self._max_concurrent)

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
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
            except Exception as e:  # pragma: no cover - dispatch loop
                log.exception("dispatch loop error: %s", e)
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
            log.exception("_process_task error: %s", e)
            await self._set_task_status(task_id, TaskStatus.FAILED, str(e))

    async def _process_link(self, task_id: int, link_id: int) -> None:
        async with self._sem:
            try:
                debrided = await self._debrid_link(task_id, link_id)
                if not debrided:
                    return
                await self._download_link(task_id, link_id, debrided)
                await self._extract_and_move(task_id, link_id)
            except Exception as e:
                log.exception("link %s failed: %s", link_id, e)
                await self._set_link_status(link_id, LinkStatus.FAILED, error=str(e))
                await self._append_task_log(task_id, f"Link {link_id} failed: {e}")
            finally:
                async with self._futures_lock:
                    self._link_futures.pop(link_id, None)

    async def _debrid_link(self, task_id: int, link_id: int) -> str | None:
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return None
            original = link.original_url
        await self._set_link_status(link_id, LinkStatus.DEBRIDDING)
        await self._append_task_log(task_id, f"Debriding: {original}")
        client = get_client()
        try:
            debrided = await client.get_debrid_link(original)
        except DebridError as e:
            await self._set_link_status(link_id, LinkStatus.FAILED, error=str(e))
            await self._append_task_log(task_id, f"Debrid failed: {e}")
            return None
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            link.debrided_url = debrided
            db.commit()
        await bus.publish("links", {"id": link_id, "task_id": task_id, "debrided_url": debrided, "status": "debrid_ok"})
        return debrided

    async def _download_link(self, task_id: int, link_id: int, url: str) -> Path:
        settings = get_settings()
        temp_root = Path(settings.temp_path)
        temp_root.mkdir(parents=True, exist_ok=True)
        target_dir = temp_root / f"task_{task_id}" / f"link_{link_id}"
        target_dir.mkdir(parents=True, exist_ok=True)

        await self._set_link_status(link_id, LinkStatus.DOWNLOADING)
        await self._append_task_log(task_id, f"Downloading into {target_dir}")

        timeout = httpx.Timeout(connect=30, read=None, write=30, pool=30)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                filename = _filename_from_response(resp) or f"download_{link_id}.bin"
                target = target_dir / filename
                total = int(resp.headers.get("content-length", "0") or 0)
                received = 0
                start = time.time()
                last_update = 0.0
                with target.open("wb") as f:
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
        await self._append_task_log(task_id, f"Download finished: {target}")
        return target

    async def _extract_and_move(self, task_id: int, link_id: int, downloaded: Path | None = None) -> None:
        await self._set_link_status(link_id, LinkStatus.EXTRACTING)
        settings = get_settings()
        temp_root = Path(settings.temp_path)
        work_dir = temp_root / f"task_{task_id}" / f"link_{link_id}"
        archive = downloaded if downloaded else find_rar_archive(work_dir)
        if not archive:
            extracted_video = find_main_video(work_dir)
        else:
            extract_dest = work_dir / "extracted"
            extract_dest.mkdir(exist_ok=True)
            with SessionLocal() as db:
                passwords = [p.value for p in db.query(Password).order_by(Password.position.asc(), Password.id.asc()).all()]
            await self._append_task_log(task_id, f"Extracting {archive.name} (passwords: {len(passwords)})")
            try:
                from .extractor import extract_archive
                extracted_video = extract_archive(archive, extract_dest, passwords)
            except Exception as e:
                raise RuntimeError(f"Extraction failed (all passwords tried): {e}") from e
        if not extracted_video or not extracted_video.exists():
            raise RuntimeError("Could not locate a video file after extraction")

        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            task = db.get(Task, task_id)
            media_type: MediaType = task.media_type
            if is_series_type(media_type):
                parsed = parse_series_name(link.filename or extracted_video.name)
            else:
                parsed = parse_series_name(link.filename or extracted_video.name)
            final = final_path_for(media_type, parsed, extracted_video.name)

        await self._set_link_status(link_id, LinkStatus.MOVING)
        await self._append_task_log(task_id, f"Moving to {final}")
        final.parent.mkdir(parents=True, exist_ok=True)
        safe_move(extracted_video, final)

        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            link.final_path = str(final)
            link.status = LinkStatus.DONE
            db.commit()
        await self._append_task_log(task_id, f"Done: {final}")
        await self._cleanup_link(task_id, link_id, work_dir)
        await bus.publish("links", {"id": link_id, "task_id": task_id, "status": "done"})

    async def _cleanup_link(self, task_id: int, link_id: int, work_dir: Path) -> None:
        safe_rmtree(work_dir)
        parent = work_dir.parent
        if parent.exists() and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except OSError:
                pass

    async def _finalize_task(self, task_id: int) -> None:
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
            await self._set_task_status(task_id, TaskStatus.FAILED, "One or more links failed")
        else:
            await self._set_task_status(task_id, TaskStatus.COMPLETED)
        await self._cleanup_task_dir(task_id)

    async def _cleanup_task_dir(self, task_id: int) -> None:
        settings = get_settings()
        task_dir = Path(settings.temp_path) / f"task_{task_id}"
        safe_rmtree(task_dir)

    async def _set_link_status(self, link_id: int, status: LinkStatus, error: str = "") -> None:
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return
            link.status = status
            if error:
                link.error = error
            db.commit()
        await bus.publish("links", {"id": link_id, "status": status.value, "error": error})

    async def _update_link_progress(self, link_id: int, progress: float, speed: float) -> None:
        with SessionLocal() as db:
            link = db.get(DownloadLink, link_id)
            if not link:
                return
            link.progress = progress
            link.speed = speed
            db.commit()
        await bus.publish("links", {"id": link_id, "progress": progress, "speed": speed})

    async def _set_task_status(self, task_id: int, status: TaskStatus, error: str = "") -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            task.status = status
            if error:
                task.log = (task.log or "") + f"\n[error] {error}"
            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.finished_at = datetime.now(timezone.utc)
            db.commit()
        await bus.publish("tasks", {"id": task_id, "status": status.value, "error": error})

    async def _append_task_log(self, task_id: int, line: str) -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            task.log = (task.log or "") + f"\n[{ts}] {line}"
            db.commit()
        await bus.publish("tasks", {"id": task_id, "log": line})


def _filename_from_response(resp: httpx.Response) -> str | None:
    cd = resp.headers.get("content-disposition", "")
    if "filename=" in cd:
        try:
            part = cd.split("filename=", 1)[1]
            part = part.strip().strip('"').strip("'")
            if part:
                return _sanitize_filename(part)
        except Exception:
            pass
    url_path = str(resp.request.url).split("?", 1)[0]
    if "/" in url_path:
        name = url_path.rsplit("/", 1)[-1]
        if name and "" != name:
            return _sanitize_filename(name)
    return None


def _sanitize_filename(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    for c in bad:
        name = name.replace(c, "_")
    return name


_manager: Manager | None = None


def get_manager() -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager()
    return _manager
