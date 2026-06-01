from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


class DLCDecodeError(Exception):
    pass


def _parse_response_body(body: str) -> list[str]:
    """Parse the dcrypt.it JSON response. The service sometimes wraps it in <textarea> tags."""
    cleaned = body.replace("<textarea>", "").replace("</textarea>", "")
    import json

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise DLCDecodeError(f"Malformed DLC service response: {e}") from e

    if isinstance(data, dict):
        if "form_errors" in data:
            errors = data["form_errors"]
            if isinstance(errors, dict):
                first = next(iter(errors.values()), "validation error")
                if isinstance(first, list) and first:
                    raise DLCDecodeError(f"DLC validation error: {first[0]}")
            raise DLCDecodeError(f"DLC validation error: {errors}")
        if data.get("success") and isinstance(data["success"], dict):
            links = data["success"].get("links")
            if isinstance(links, list):
                return [str(u) for u in links]
        if "error" in data:
            raise DLCDecodeError(f"DLC service error: {data['error']}")
    raise DLCDecodeError("Malformed DLC service response: no links found")


def _client() -> httpx.Client:
    return httpx.Client(timeout=120.0, headers={"User-Agent": "plex-get/1.0"})


def _base() -> str:
    return get_settings().dcrypt_base_url.rstrip("/")


def decrypt_paste(content: str) -> list[str]:
    """Decrypt a .dlc file from its raw text content via dcrypt.it's /decrypt/paste endpoint."""
    with _client() as client:
        resp = client.post(f"{_base()}/decrypt/paste", data={"content": content})
        resp.raise_for_status()
        return _parse_response_body(resp.text)


def decrypt_upload(path: Path) -> list[str]:
    """Decrypt a .dlc file by uploading it via dcrypt.it's /decrypt/upload endpoint."""
    with _client() as client:
        with path.open("rb") as f:
            resp = client.post(
                f"{_base()}/decrypt/upload",
                files={"dlcfile": (path.name, f, "application/octet-stream")},
            )
        resp.raise_for_status()
        return _parse_response_body(resp.text)


def decrypt_container_link(link: str) -> list[str]:
    """Decrypt a .dlc file hosted at the given URL via dcrypt.it's /decrypt/container endpoint."""
    with _client() as client:
        resp = client.post(f"{_base()}/decrypt/container", data={"link": link})
        resp.raise_for_status()
        return _parse_response_body(resp.text)


def parse_dlc_file(path: Path) -> list[str]:
    """Parse a .dlc container file by sending it to the dcrypt.it service."""
    return decrypt_upload(path)


def parse_dlc_text(text: str) -> list[str]:
    """Parse the contents of a .dlc file via the dcrypt.it paste endpoint."""
    return decrypt_paste(text)


_URL_RE = __import__("re").compile(r"(?:https?|ftp)://[^\s<>\"'`,;]+", __import__("re").IGNORECASE)


def parse_input(raw: str, dlc_files: Iterable[Path] = ()) -> list[str]:
    """Extract a list of URLs from a freeform text block and/or .dlc files.

    - Plain text containing http(s)/ftp URLs is scanned with a regex so any
      whitespace/commas/newlines separating URLs work.
    - .dlc files are decrypted via the dcrypt.it service.
    """
    links: list[str] = []
    if raw:
        for match in _URL_RE.finditer(raw):
            url = match.group(0).rstrip(".,;:!?)\"")
            if url:
                links.append(url)
    for dlc in dlc_files:
        links.extend(parse_dlc_file(dlc))
    seen = set()
    deduped: list[str] = []
    for url in links:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped
