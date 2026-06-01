from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import get_settings
from .db import SessionLocal
from .models import Setting, Task

log = logging.getLogger(__name__)


async def notify_task(task_id: int, outcome: str, message: str) -> None:
    """Send a Discord webhook notification for a task outcome.

    Reads the webhook URL from the Setting table (key='discord_webhook_url').
    No-op if not configured. Failures are logged at debug level only.
    """
    try:
        with SessionLocal() as db:
            url_row = db.get(Setting, 'discord_webhook_url')
            url = (url_row.value if url_row else '').strip()
            task = db.get(Task, task_id)
        if not url:
            return
        title = task.title if task and task.title else f'Task {task_id}'
        media_type = task.media_type.value if task else '?'
        color = 0x3EC46C if outcome == 'completed' else 0xE0524D
        payload = {
            'embeds': [
                {
                    'title': f'Plex-Get task #{task_id} {outcome}',
                    'description': message,
                    'color': color,
                    'fields': [
                        {'name': 'Title', 'value': title, 'inline': True},
                        {'name': 'Type', 'value': media_type, 'inline': True},
                    ],
                }
            ]
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except Exception as e:
        log.debug('discord notify failed: %s', e)


def disk_usage_summary() -> dict:
    """Return free/total bytes for the temp path and each media path."""
    import shutil
    s = get_settings()
    out: dict = {'temp': _stat(s.temp_path), 'media': {}}
    for label, p in {
        'movie': s.media_path_movies,
        'series': s.media_path_series,
        'anime_movie': s.media_path_anime_movies,
        'anime_series': s.media_path_anime_series,
        'uncategorized': s.media_path_uncategorized,
    }.items():
        out['media'][label] = _stat(p)
    return out


def _stat(path: str) -> Optional[dict]:
    try:
        u = shutil.disk_usage(path)
        return {'path': path, 'free': u.free, 'total': u.total, 'used': u.used}
    except Exception:
        return {'path': path, 'free': None, 'total': None, 'used': None, 'error': 'unavailable'}
