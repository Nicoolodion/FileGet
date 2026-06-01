from __future__ import annotations

import base64
import binascii
import struct
import zlib
from pathlib import Path
from typing import Iterable


class DLCDecodeError(Exception):
    pass


def _decode_container(b64: str) -> list[list[str]]:
    try:
        raw = base64.b64decode(b64)
    except binascii.Error as e:
        raise DLCDecodeError(f"Invalid base64: {e}") from e
    try:
        decompressed = zlib.decompress(raw)
    except zlib.error as e:
        raise DLCDecodeError(f"Invalid zlib payload: {e}") from e

    containers: list[list[str]] = []
    idx = 0
    while idx < len(decompressed):
        if decompressed[idx] != 0x01:
            raise DLCDecodeError("Missing container header")
        idx += 1
        url_count = decompressed[idx]
        idx += 1
        urls: list[str] = []
        for _ in range(url_count):
            if decompressed[idx] != 0x01:
                raise DLCDecodeError("Missing url header")
            idx += 1
            url_len = struct.unpack(">H", decompressed[idx:idx + 2])[0]
            idx += 2
            url = decompressed[idx:idx + url_len].decode("utf-8", errors="replace")
            idx += url_len
            urls.append(url)
        containers.append(urls)
    return containers


def parse_dlc_file(path: Path) -> list[str]:
    """Parse a .dlc container file and return the flattened list of links."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_dlc_text(text)


def parse_dlc_text(text: str) -> list[str]:
    """Parse a .dlc file content. Each non-empty line is treated as a container."""
    all_links: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for container in _decode_container(line):
            all_links.extend(container)
    return all_links


def parse_input(raw: str, dlc_files: Iterable[Path] = ()) -> list[str]:
    """Extract a list of URLs from a freeform text block and/or .dlc files."""
    links: list[str] = []
    if raw:
        for token in raw.replace(",", "\n").split():
            token = token.strip()
            if not token:
                continue
            if token.lower().startswith(("http://", "https://", "ftp://")):
                links.append(token)
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.lower().startswith(("http://", "https://", "ftp://")):
                pass
    for dlc in dlc_files:
        links.extend(parse_dlc_file(dlc))
    seen = set()
    deduped: list[str] = []
    for url in links:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped
