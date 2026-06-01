from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import rarfile

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts', '.webm'}


def _is_video(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTENSIONS


_PART_RE = re.compile(r'\.part(\d+)\.rar$', re.IGNORECASE)


def volume_group_key(filename: str) -> Tuple[str, int]:
    """Return (base, part_index) for a RAR filename. part_index=0 means standalone.

    Examples:
      foo.part1.rar -> ('foo.rar', 1)
      foo.part2.rar -> ('foo.rar', 2)
      foo.rar       -> ('foo.rar', 0)
    """
    m = _PART_RE.search(filename)
    if m:
        base = filename[: m.start()] + '.rar'
        return base, int(m.group(1))
    lower = filename.lower()
    if lower.endswith('.rar'):
        return filename, 0
    return filename, 0


def find_rar_archive(directory: Path) -> Optional[Path]:
    parts: list[Path] = sorted(directory.rglob('*.rar'))
    if not parts:
        return None
    parts.sort(key=lambda p: 0 if p.name.lower() == 'video_ts.rar' else 1)
    for p in parts:
        if p.name.lower().startswith('._') or p.name.lower() == 'video_ts.rar':
            continue
        return p
    return parts[0] if parts else None


def group_rar_volumes(files: Sequence[Path]) -> List[List[Path]]:
    """Group RAR files by volume-set. Each group is sorted by part index (1, 2, 3, ...).

    Standalone archives (no .partN suffix) come back as single-element groups.
    """
    groups: dict[str, list[Path]] = {}
    standalone: list[Path] = []
    for f in files:
        base, idx = volume_group_key(f.name)
        if idx > 0:
            groups.setdefault(base, []).append(f)
        else:
            standalone.append(f)
    out: list[list[Path]] = []
    for base, parts in groups.items():
        parts_sorted = sorted(parts, key=lambda p: volume_group_key(p.name)[1])
        out.append(parts_sorted)
    for s in standalone:
        out.append([s])
    out.sort(key=lambda g: g[0].name.lower())
    return out


def _largest_video_in_dir(directory: Path) -> Optional[Path]:
    candidates = [p for p in directory.rglob('*') if p.is_file() and _is_video(p)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _largest_video_in_rar(rf: rarfile.RarFile) -> Optional[rarfile.RarInfo]:
    candidates = [info for info in rf.infolist() if not info.is_dir() and Path(info.filename).suffix.lower() in VIDEO_EXTENSIONS]
    if not candidates:
        return None
    return max(candidates, key=lambda i: i.file_size)


class ArchiveCorrupt(Exception):
    """Raised when a RAR is structurally invalid (truncated, bad CRC, etc.) and is
    not the result of an incorrect password."""


def extract_archive(
    archive: Path,
    destination: Path,
    passwords: list[str],
    *,
    first_volume: Optional[Path] = None,
) -> Path:
    """Extract a (multi-volume) RAR archive. When *first_volume* is supplied (i.e. the
    `.part1.rar` of a set), rarfile uses it as the entry-point and automatically follows
    the subsequent `.partN.rar` siblings on disk.

    Passwords are tried in order. Each attempt's output is verified: if the
    largest extracted file is suspiciously small (less than 1% of the
    archive's claimed file size), we treat it as a wrong-password / bad
    extraction and try the next password. Some backends (notably unar with
    RAR5 + wrong password) silently produce 0-byte output instead of raising.
    """
    destination.mkdir(parents=True, exist_ok=True)
    entry = first_volume or archive
    last_error: Optional[Exception] = None
    # Determine the expected extracted file size from the archive header so we
    # can detect 'silent zero-byte output' failures.
    expected_size = _archive_uncompressed_size(entry)
    log.info('Extracting %s (entry=%s, expected_uncompressed=%s, backend=%s)', archive, entry, expected_size, rarfile.UNRAR_TOOL)
    for pw in [''] + passwords:
        try:
            with rarfile.RarFile(str(entry)) as rf:
                if pw:
                    rf.setpassword(pw)
                rf.extractall(str(destination))
            # Verify: the largest extracted file should be > 1% of the
            # archive's claimed uncompressed size. If we get a tiny / 0-byte
            # file, the password is almost certainly wrong - keep trying.
            largest = _largest_video_in_dir(destination)
            if largest is None:
                largest = _any_file_in_dir(destination)
            if largest is None or largest.stat().st_size == 0:
                log.warning('Password %r produced no output - trying next', pw)
                _clear_destination(destination)
                continue
            if expected_size and largest.stat().st_size < expected_size * 0.01:
                log.warning('Password %r produced %d bytes (expected ~%d) - trying next', pw, largest.stat().st_size, expected_size)
                _clear_destination(destination)
                continue
            return largest
        except rarfile.PasswordRequired as e:
            last_error = e
            log.debug('Rar extract needs password (pw=%r): %s', pw, e)
            _clear_destination(destination)
            continue
        except (rarfile.BadRarFile, Exception) as e:
            last_error = e
            log.debug('Rar extract failed (pw=%r): %s', pw, e)
            _clear_destination(destination)
            continue
    if last_error:
        raise last_error
    raise RuntimeError('Extraction failed for unknown reason')


def _any_file_in_dir(directory: Path) -> Optional[Path]:
    try:
        files = [p for p in directory.rglob('*') if p.is_file()]
    except Exception:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)


def _clear_destination(directory: Path) -> None:
    import shutil as _sh
    try:
        for p in directory.iterdir():
            if p.is_file() or p.is_symlink():
                try:
                    p.unlink()
                except OSError:
                    pass
            elif p.is_dir():
                try:
                    _sh.rmtree(p)
                except OSError:
                    pass
    except Exception:
        pass


def _archive_uncompressed_size(archive: Path) -> int:
    """Return the total uncompressed size declared in the archive header (0 if unknown)."""
    try:
        with rarfile.RarFile(str(archive)) as rf:
            total = 0
            for info in rf.infolist():
                if not info.is_dir():
                    total += getattr(info, 'file_size', 0) or 0
            return total
    except Exception:
        return 0


def _resolve_extracted_video(directory: Path) -> Path:
    for p in directory.rglob('*'):
        if p.is_file() and _is_video(p):
            return p
    raise FileNotFoundError(f'No video file found in {directory}')


def find_main_video(extract_dir: Path) -> Path:
    direct = _largest_video_in_dir(extract_dir)
    if direct:
        return direct
    archive = find_rar_archive(extract_dir)
    if not archive:
        raise FileNotFoundError('No video file or archive found after extraction')
    with rarfile.RarFile(str(archive)) as rf:
        info = _largest_video_in_rar(rf)
    if not info:
        raise FileNotFoundError('No video file inside the archive')
    nested = extract_dir / '_nested'
    nested.mkdir(exist_ok=True)
    with rarfile.RarFile(str(archive)) as rf:
        rf.extract(info, str(nested), pwd=None)
    return _largest_video_in_dir(nested) or _resolve_extracted_video(nested)


def safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    try:
        import shutil
        shutil.rmtree(path)
    except OSError as e:
        log.warning('Failed to remove %s: %s', path, e)


def safe_move(src: Path, dst: Path) -> None:
    import shutil
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        base = dst.stem
        suffix = dst.suffix
        i = 1
        while True:
            candidate = dst.parent / f'{base} ({i}){suffix}'
            if not candidate.exists():
                dst = candidate
                break
            i += 1
    shutil.move(str(src), str(dst))


def configure_rarfile() -> None:
    import os
    import shutil
    # Prefer the real unrar over unar: unar is known to silently produce
    # 0-byte output for some RAR5 encrypted volumes when the password is
    # slightly off, while unrar raises a clear error. unrar is downloaded
    # at build time via /usr/local/bin/unrar (see Dockerfile).
    if shutil.which('unrar'):
        rarfile.UNRAR_TOOL = 'unrar'
    elif shutil.which('unar'):
        rarfile.UNRAR_TOOL = 'unar'
    else:
        rarfile.UNRAR_TOOL = 'unrar'
    rarfile.PATH_SEP = os.sep


def list_rar_volumes(directory: Path) -> List[Path]:
    return sorted(p for p in directory.rglob('*.rar') if not p.name.lower().startswith('._'))


def expected_volume_count(archive: Path) -> Optional[int]:
    """Try to detect how many volumes make up a multi-part RAR set by inspecting the
    `comment` / header of the first volume. Returns None when not determinable.
    """
    try:
        with rarfile.RarFile(str(archive)) as rf:
            volumes = getattr(rf, 'volumes', None) or []
            if volumes:
                return len(volumes)
    except Exception:
        return None
    return None
