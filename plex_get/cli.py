from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

from .config import get_settings
from .db import SessionLocal, init_db
from .dlc import parse_input, parse_dlc_file
from .debrid import get_client, DebridError
from .manager import get_manager
from .models import DownloadLink, LinkStatus, MediaType, Password, Task, TaskStatus

console = Console()


def _media_type(value: str) -> MediaType:
    aliases = {
        "series": MediaType.SERIES,
        "movie": MediaType.MOVIE,
        "movies": MediaType.MOVIE,
        "anime-series": MediaType.ANIME_SERIES,
        "anime_series": MediaType.ANIME_SERIES,
        "anime-series": MediaType.ANIME_SERIES,
        "anime-movie": MediaType.ANIME_MOVIE,
        "anime_movies": MediaType.ANIME_MOVIE,
        "uncategorized": MediaType.UNCATEGORIZED,
    }
    key = value.lower().replace(" ", "-")
    if key not in aliases:
        raise click.BadParameter(f"Unknown media type: {value}")
    return aliases[key]


@click.group()
def cli() -> None:
    """Plex-Get command line interface."""


@cli.command()
def init() -> None:
    """Initialize the database."""
    init_db()
    click.echo("Database initialized.")


@cli.command()
@click.option("--type", "media_type", required=True, help="series|movie|anime-series|anime-movies|uncategorized")
@click.option("--title", default="", help="Optional title")
@click.option("--dlc", "dlc_files", multiple=True, type=click.Path(exists=True, path_type=Path))
@click.argument("links", nargs=-1)
def new(media_type: str, title: str, dlc_files: tuple[Path, ...], links: tuple[str, ...]) -> None:
    """Create a new task. Pass links as args or via --dlc file (or stdin pipe into 'links')."""
    init_db()
    raw = "\n".join(links)
    mt = _media_type(media_type)
    urls = parse_input(raw, dlc_files=dlc_files)
    if not urls:
        raise click.ClickException("No links found.")
    with SessionLocal() as db:
        task = Task(media_type=mt, status=TaskStatus.AWAITING_CONFIRMATION, title=title, raw_input=raw)
        db.add(task)
        db.commit()
        db.refresh(task)
        for u in urls:
            db.add(DownloadLink(task_id=task.id, original_url=u, status=LinkStatus.PENDING))
        db.commit()
        db.refresh(task)
        click.echo(f"Created task #{task.id} with {len(urls)} link(s). Debriding...")
        client = get_client()
        async def debrid_all():
            results = []
            for link in task.links:
                try:
                    link.debrided_url = await client.get_debrid_link(link.original_url)
                    results.append((link, True, ""))
                except DebridError as e:
                    results.append((link, False, str(e)))
            db.commit()
            return results
        results = asyncio.run(debrid_all())
        all_ok = all(r[1] for r in results)
        for link, ok, err in results:
            click.echo(f"  [{ 'ok' if ok else 'FAIL' }] {link.original_url} {('- ' + err) if err else ''}")
        if not all_ok:
            click.echo("Some links failed. Aborting.")
            return
        click.echo("Press Enter to start downloading (Ctrl-C to cancel)...")
        try:
            input()
        except KeyboardInterrupt:
            click.echo("Cancelled. Task kept in awaiting state.")
            return
        task.status = TaskStatus.QUEUED
        db.commit()
        click.echo(f"Task #{task.id} queued.")


@cli.command(name="list")
@click.option("--last-24h", is_flag=True, help="Show only finished tasks in the last 24h")
def list_cmd(last_24h: bool) -> None:
    """List active and recent tasks."""
    init_db()
    from datetime import timedelta, timezone
    with SessionLocal() as db:
        q = db.query(Task).order_by(Task.created_at.desc())
        if last_24h:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            q = q.filter(Task.finished_at >= cutoff)
        tasks = q.limit(20).all()
        for t in tasks:
            click.echo(f"#{t.id}  [{t.status.value}]  {t.media_type.value}  - {len(t.links)} links  - {t.title or ''}")


@cli.command()
@click.argument("task_id", type=int)
def watch(task_id: int) -> None:
    """Live preview of a task (downloads & progress)."""
    init_db()
    asyncio.run(_watch(task_id))


async def _watch(task_id: int) -> None:
    from .events import bus
    manager = get_manager()
    await manager.start()
    q = await bus.subscribe("*")
    with Live(refresh_per_second=4, console=console) as live:
        async def render_loop():
            while True:
                with SessionLocal() as db:
                    t = db.get(Task, task_id)
                    if not t:
                        live.update(Panel(f"Task {task_id} not found."))
                        return
                    table = Table(title=f"Task #{task_id}  [{t.status.value}]  {t.media_type.value}  {t.title or ''}", expand=True)
                    table.add_column("#", justify="right")
                    table.add_column("URL", overflow="fold")
                    table.add_column("Status")
                    table.add_column("Progress", justify="right")
                    table.add_column("Speed", justify="right")
                    table.add_column("Final path", overflow="fold")
                    for l in t.links:
                        speed = f"{l.speed/1024/1024:.2f} MB/s" if l.speed else ""
                        table.add_row(str(l.id), l.original_url, l.status.value, f"{l.progress*100:.1f}%", speed, l.final_path or "")
                    log_text = (t.log or "").splitlines()[-15:]
                    panel = Panel("\n".join(log_text) or "(no log)", title="Log (last 15 lines)")
                    group = console.group(table, panel)
                live.update(table)
                if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    return
                await asyncio.sleep(0.4)
        await asyncio.gather(render_loop(), _consume(q))


async def _consume(q):
    try:
        while True:
            await q.get()
    except Exception:
        return


@cli.command()
@click.option("--concurrency", type=int, default=None, help="Override concurrent downloads")
@click.option("--host", default="0.0.0.0")
@click.option("--port", type=int, default=None)
def serve(concurrency: int | None, host: str, port: int | None) -> None:
    """Start the web UI server."""
    import uvicorn
    init_db()
    s = get_settings()
    if concurrency:
        s.max_concurrent_downloads = concurrency
    p = port or s.web_port
    click.echo(f"Starting Plex-Get web UI on http://{host}:{p}")
    uvicorn.run("plex_get.api:app", host=host, port=p, reload=False)


@cli.group()
def password() -> None:
    """Manage archive passwords."""


@password.command(name="list")
def pw_list() -> None:
    init_db()
    with SessionLocal() as db:
        items = db.query(Password).order_by(Password.position.asc(), Password.id.asc()).all()
        for p in items:
            click.echo(f"#{p.id}  {p.value}")


@password.command(name="add")
@click.argument("value")
def pw_add(value: str) -> None:
    init_db()
    with SessionLocal() as db:
        exists = db.query(Password).filter(Password.value == value).first()
        if exists:
            click.echo("Already exists.")
            return
        max_pos = db.query(Password).count()
        db.add(Password(value=value, position=max_pos))
        db.commit()
        click.echo("Added.")


@password.command(name="remove")
@click.argument("password_id", type=int)
def pw_remove(password_id: int) -> None:
    init_db()
    with SessionLocal() as db:
        p = db.get(Password, password_id)
        if not p:
            raise click.ClickException("Not found")
        db.delete(p)
        db.commit()
        click.echo("Removed.")


if __name__ == "__main__":
    cli()
