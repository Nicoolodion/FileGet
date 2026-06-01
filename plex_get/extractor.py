from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

import rarfile

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".webm"}


def _is_video(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTENSIONS


def find_rar_archive(directory: Path) -> Optional[Path]:
    parts: list[Path] = sorted(directory.rglob("*.rar"))
    if not parts:
        return None
    parts.sort(key=lambda p: 0 if p.name.lower() == "video_ts.rar" else 1)
    for p in parts:
        if p.name.lower().startswith("._") or p.name.lower() == "video_ts.rar":
            continue
        return p
    return parts[0] if parts else None


def _largest_video_in_dir(directory: Path) -> Optional[Path]:
    candidates = [p for p in directory.rglob("*") if p.is_file() and _is_video(p)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _largest_video_in_rar(rf: rarfile.RarFile) -> Optional[rarfile.RarInfo]:
    candidates = [info for info in rf.infolist() if not info.is_dir() and Path(info.filename).suffix.lower() in VIDEO_EXTENSIONS]
    if not candidates:
        return None
    return max(candidates, key=lambda i: i.file_size)


def extract_archive(
    archive: Path,
    destination: Path,
    passwords: list[str],
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    last_error: Optional[Exception] = None
    for pw in [""] + passwords:
        try:
            with rarfile.RarFile(str(archive)) as rf:
                if pw:
                    rf.setpassword(pw)
                rf.extractall(str(destination))
            return _largest_video_in_dir(destination) or _resolve_extracted_video(destination)
        except (rarfile.BadRarFile, rarfile.PasswordRequired, Exception) as e:
            last_error = e
            log.debug("Rar extract attempt failed (pw=%r): %s", pw, e)
            continue
    if last_error:
        raise last_error
    raise RuntimeError("Extraction failed for unknown reason")


def _resolve_extracted_video(directory: Path) -> Path:
    for p in directory.rglob("*"):
        if p.is_file() and _is_video(p):
            return p
    raise FileNotFoundError(f"No video file found in {directory}")


def find_main_video(extract_dir: Path) -> Path:
    direct = _largest_video_in_dir(extract_dir)
    if direct:
        return direct
    archive = find_rar_archive(extract_dir)
    if not archive:
        raise FileNotFoundError("No video file or archive found after extraction")
    with rarfile.RarFile(str(archive)) as rf:
        info = _largest_video_in_rar(rf)
    if not info:
        raise FileNotFoundError("No video file inside the archive")
    nested = extract_dir / "_nested"
    nested.mkdir(exist_ok=True)
    with rarfile.RarFile(str(archive)) as rf:
        rf.extract(info, str(nested), pwd=None)
    return _largest_video_in_dir(nested) or _resolve_extracted_video(nested)


def safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as e:
        log.warning("Failed to remove %s: %s", path, e)


def safe_move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        base = dst.stem
        suffix = dst.suffix
        i = 1
        while True:
            candidate = dst.parent / f"{base} ({i}){suffix}"
            if not candidate.exists():
                dst = candidate
                break
            i += 1
    shutil.move(str(src), str(dst))


def configure_rarfile() -> None:
    if shutil.which("unrar"):
        rarfile.UNRAR_TOOL = "unrar"
    elif shutil.which("unar"):
        rarfile.UNRAR_TOOL = "unar"
    else:
        rarfile.UNRAR_TOOL = "unrar"
    rarfile.PATH_SEP = os.sep
