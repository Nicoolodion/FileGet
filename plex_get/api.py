from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db, init_db
from .dlc import parse_input
from .events import bus
from .manager import get_manager
from .models import DownloadLink, MediaType, Password, Task, TaskStatus, LinkStatus
from .schemas import PasswordIn, PasswordOut, TaskCreate, TaskOut, LinkOut

app = FastAPI(title="Plex-Get")
STATIC_DIR = Path(__file__).parent / "web" / "static"
TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    await get_manager().start()


def _require_auth(request: Request) -> None:
    s = get_settings()
    if not s.web_username or not s.web_password:
        return
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})
    import base64
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, _, pw = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="Bad credentials", headers={"WWW-Authenticate": "Basic"})
    if user != s.web_username or pw != s.web_password:
        raise HTTPException(status_code=401, detail="Bad credentials", headers={"WWW-Authenticate": "Basic"})


def _link_to_out(l: DownloadLink) -> LinkOut:
    return LinkOut(
        id=l.id,
        original_url=l.original_url,
        debrided_url=l.debrided_url,
        filename=l.filename,
        final_path=l.final_path,
        status=l.status,
        progress=l.progress,
        speed=l.speed,
        error=l.error,
    )


def _task_to_out(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id,
        media_type=t.media_type,
        status=t.status,
        title=t.title,
        raw_input=t.raw_input,
        created_at=t.created_at,
        updated_at=t.updated_at,
        finished_at=t.finished_at,
        log=t.log,
        links=[_link_to_out(l) for l in t.links],
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    _require_auth(request)
    html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/static/{path:path}")
async def static_files(path: str, request: Request):
    _require_auth(request)
    f = STATIC_DIR / path
    if not f.exists() or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/api/tasks")
async def list_tasks(
    request: Request,
    include_last_hours: Optional[int] = None,
    db: Session = Depends(get_db),
):
    _require_auth(request)
    q = db.query(Task)
    if include_last_hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=include_last_hours)
        q = q.filter(
            (Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROCESSING, TaskStatus.AWAITING_CONFIRMATION]))
            | ((Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED])) & (Task.finished_at >= cutoff))
        )
    tasks = q.order_by(Task.created_at.desc()).all()
    return [_task_to_out(t).model_dump(mode="json") for t in tasks]


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return _task_to_out(t).model_dump(mode="json")


@app.post("/api/tasks")
async def create_task(payload: TaskCreate, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    task = Task(
        media_type=payload.media_type,
        status=TaskStatus.AWAITING_CONFIRMATION,
        title=payload.title or "",
        raw_input=payload.raw_input,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    urls = parse_input(payload.raw_input)
    for u in urls:
        db.add(DownloadLink(task_id=task.id, original_url=u, status=LinkStatus.PENDING))
    db.commit()
    db.refresh(task)
    return _task_to_out(task).model_dump(mode="json")


@app.post("/api/tasks/{task_id}/upload-dlc")
async def upload_dlc(task_id: int, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    _require_auth(request)
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    content = (await file.read()).decode("utf-8", errors="replace")
    from .dlc import parse_dlc_text
    urls = parse_dlc_text(content)
    for u in urls:
        db.add(DownloadLink(task_id=t.id, original_url=u, status=LinkStatus.PENDING))
    db.commit()
    db.refresh(t)
    return _task_to_out(t).model_dump(mode="json")


@app.post("/api/tasks/{task_id}/debrid")
async def debrid_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    from .debrid import get_client, DebridError
    client = get_client()
    results = []
    for link in t.links:
        if link.debrided_url:
            results.append({"id": link.id, "ok": True, "skipped": True})
            continue
        try:
            debrided = await client.get_debrid_link(link.original_url)
            link.debrided_url = debrided
            results.append({"id": link.id, "ok": True})
        except DebridError as e:
            results.append({"id": link.id, "ok": False, "error": str(e)})
    db.commit()
    db.refresh(t)
    all_ok = all(r.get("ok") for r in results)
    return {"all_ok": all_ok, "results": results, "task": _task_to_out(t).model_dump(mode="json")}


@app.post("/api/tasks/{task_id}/start")
async def start_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    if t.status not in (TaskStatus.AWAITING_CONFIRMATION, TaskStatus.FAILED):
        raise HTTPException(400, f"Task cannot be started in status {t.status.value}")
    t.status = TaskStatus.QUEUED
    db.commit()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/close")
async def close_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        raise HTTPException(400, "Task is not finished yet")
    t.status = TaskStatus.QUEUED
    t.finished_at = None
    db.commit()
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    db.delete(t)
    db.commit()
    return {"ok": True}


@app.get("/api/passwords")
async def list_passwords(request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    items = db.query(Password).order_by(Password.position.asc(), Password.id.asc()).all()
    return [PasswordOut.model_validate(p).model_dump() for p in items]


@app.post("/api/passwords")
async def add_password(payload: PasswordIn, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    if not payload.value:
        raise HTTPException(400, "Password cannot be empty")
    exists = db.query(Password).filter(Password.value == payload.value).first()
    if exists:
        raise HTTPException(400, "Password already exists")
    max_pos = db.query(Password).count()
    pw = Password(value=payload.value, position=max_pos)
    db.add(pw)
    db.commit()
    db.refresh(pw)
    return PasswordOut.model_validate(pw).model_dump()


@app.delete("/api/passwords/{password_id}")
async def remove_password(password_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    pw = db.get(Password, password_id)
    if not pw:
        raise HTTPException(404)
    db.delete(pw)
    db.commit()
    return {"ok": True}


@app.post("/api/passwords/reorder")
async def reorder_passwords(request: Request, order: list[int], db: Session = Depends(get_db)):
    _require_auth(request)
    passwords = {p.id: p for p in db.query(Password).all()}
    for idx, pid in enumerate(order):
        if pid in passwords:
            passwords[pid].position = idx
    db.commit()
    return {"ok": True}


@app.get("/api/settings")
async def list_settings(request: Request, db: Session = Depends(get_db)):
    _require_auth(request)
    s = get_settings()
    return {
        "max_concurrent_downloads": s.max_concurrent_downloads,
        "media_paths": {
            "movie": s.media_path_movies,
            "series": s.media_path_series,
            "anime_movie": s.media_path_anime_movies,
            "anime_series": s.media_path_anime_series,
            "uncategorized": s.media_path_uncategorized,
        },
        "temp_path": s.temp_path,
        "media_types": [m.value for m in MediaType],
    }


@app.post("/api/settings/concurrency")
async def set_concurrency(request: Request, value: int):
    _require_auth(request)
    get_manager().set_concurrency(value)
    return {"ok": True, "value": value}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    q = await bus.subscribe("*")
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_text(payload)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"channel": "_ping", "data": {}}))
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe("*", q)
