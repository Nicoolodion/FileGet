from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import get_settings
from .models import MediaType


@dataclass
class ParsedName:
    show: str
    season: Optional[int]
    episode: Optional[int]
    is_special: bool
    raw: str


_RANGE_RE = re.compile(r"\.s(\d{1,2})e(\d{1,2})", re.IGNORECASE)
_SEASON_ONLY_RE = re.compile(r"\.s(\d{1,2})(?:[\.\- ])", re.IGNORECASE)
_SPECIAL_RE = re.compile(r"\.s(\d{1,2})\.specials?\.", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def parse_series_name(filename: str) -> ParsedName:
    name = filename
    for ext in (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break

    show_part = name
    season: Optional[int] = None
    episode: Optional[int] = None
    is_special = False

    m = _RANGE_RE.search(name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        show_part = name[: m.start()]
    else:
        m2 = _SPECIAL_RE.search(name)
        if m2:
            season = int(m2.group(1))
            is_special = True
            show_part = name[: m2.start()]
        else:
            m3 = _SEASON_ONLY_RE.search(name)
            if m3:
                season = int(m3.group(1))
                show_part = name[: m3.start()]

    show = re.sub(r"[._]+", " ", show_part).strip()
    show = re.sub(r"\s+", " ", show)
    show = show.strip(" -")
    return ParsedName(show=show, season=season, episode=episode, is_special=is_special, raw=filename)


def parse_movie_name(filename: str) -> tuple[str, Optional[int]]:
    name = filename
    for ext in (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    m = _YEAR_RE.search(name)
    year = int(m.group(0)) if m else None
    title = name if not m else name[: m.start()]
    title = re.sub(r"[._]+", " ", title).strip()
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title, year


def is_series_type(media_type: MediaType) -> bool:
    return media_type in (MediaType.SERIES, MediaType.ANIME_SERIES)


def destination_subfolder(media_type: MediaType, parsed: ParsedName) -> Path:
    """For a series/anime-series: the per-episode subfolder path (relative to the type root)."""
    if not is_series_type(media_type):
        raise ValueError("destination_subfolder only applies to series-like media types")
    show_dir = sanitize_dirname(parsed.show)
    if parsed.is_special or parsed.season is None:
        season_dir = "Specials"
    else:
        season_dir = f"Season {parsed.season:02d}"
    return Path(show_dir) / season_dir


def movie_folder(media_type: MediaType, parsed_filename: str) -> Path:
    title, year = parse_movie_name(parsed_filename)
    folder = sanitize_dirname(title)
    if year:
        folder = f"{folder} ({year})"
    return Path(folder)


def sanitize_dirname(name: str) -> str:
    name = name.strip().strip(".")
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", name)
    return name.strip()


def final_path_for(media_type: MediaType, parsed: ParsedName, video_filename: str) -> Path:
    settings = get_settings()
    base = settings.media_path_for(media_type.value)
    if is_series_type(media_type):
        sub = destination_subfolder(media_type, parsed)
        return base / sub / sanitize_filename(video_filename)
    if media_type in (MediaType.MOVIE, MediaType.ANIME_MOVIE):
        folder = movie_folder(media_type, parsed.raw)
        return base / folder / sanitize_filename(video_filename)
    return base / sanitize_filename(video_filename)


def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", name)
    return name
