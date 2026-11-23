"""Cutting a video too big for one file of a flash transfer.

    from backend.flash.cut import cut
    files = cut(video, staging_folder)
    # -> [video] when it fits into one file; else its parts, written into staging_folder:
    #    "[20261122][森野鈴] 标题 (2／3 03.12.45–06.20.10).mp4"

The parts are cut without re-encoding (ffmpeg's stream copy), so each begins at a key frame
and plays by itself. The caller owns the folder and removes it with the parts.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# QQ refuses a file of a flash transfer over 10 GB (verified by the owner). Decimal GB: if QQ
# counts in GiB, a part is only a little smaller than it had to be.
LIMIT = 10_000_000_000
# The size a part is planned at: a stream copy cuts at key frames and the bit rate is uneven,
# so a part planned at the limit itself would often come out over it.
CUTTING_SIZE = 9_500_000_000
# A part over the limit makes the plan again with one part more. An uneven bit rate costs a
# part or two; a part still over the limit after this many more lies between key frames
# too far apart for any cut to split.
MORE_PARTS = 3


class CutError(Exception):
    """ffmpeg or ffprobe failed, or no cut brought every part under the limit."""


def cut(video: Path, folder: Path, *, limit: int = LIMIT, cutting_size: int = CUTTING_SIZE,
        ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> list[Path]:
    """The files `video` goes as in a flash transfer: `[video]`, untouched, when it fits into
    one file; else its parts, cut into `folder` (made when missing), in order. Raises
    CutError, leaving no part behind, when the cutting fails."""
    size = video.stat().st_size
    if size <= limit:
        return [video]
    duration = _duration(video, ffprobe)
    count = len(plan(size, duration, limit, cutting_size))
    folder.mkdir(parents=True, exist_ok=True)
    for count in range(count, count + MORE_PARTS + 1):
        with tempfile.TemporaryDirectory(prefix=".cutting-", dir=folder) as tmp:
            pieces = _split(video, Path(tmp), equal_parts(duration, count), ffmpeg)
            if all(p.stat().st_size <= limit for p in pieces):
                parts = _name(pieces, video, folder, ffprobe)
                log.info("cut %s into %d parts", video.name, len(parts))
                return parts
        log.info("cutting %s into %d parts left one over the limit; trying %d", video.name, count, count + 1)
    raise CutError(f"{video.name}: a part is still over the limit in {count} parts")


def plan(size: int, duration: float, limit: int = LIMIT, cutting_size: int = CUTTING_SIZE) -> list[tuple[float, float]]:
    """The parts a video of `size` bytes and `duration` seconds is cut into, as (start, end)
    in seconds: the whole video when it fits into one file, else the fewest parts of equal
    length whose expected size is under `cutting_size`."""
    if size <= limit:
        return [(0.0, duration)]
    return equal_parts(duration, size // cutting_size + 1)


def equal_parts(duration: float, count: int) -> list[tuple[float, float]]:
    return [(duration * i / count, duration * (i + 1) / count) for i in range(count)]


def part_name(stem: str, suffix: str, number: int, count: int, start: float, end: float) -> str:
    """`<stem> (2／3 03.12.45–06.20.10)<suffix>`: the part's number, the count and where in
    the live it begins and ends, in whole seconds."""
    return f"{stem} ({number}／{count} {_clock(start)}–{_clock(end)}){suffix}"


def _clock(seconds: float) -> str:
    """Hours, minutes and seconds joined by dots: Windows refuses a colon in a file name."""
    s = int(seconds)
    return f"{s // 3600:02d}.{s // 60 % 60:02d}.{s % 60:02d}"


def _split(video: Path, out: Path, parts: list[tuple[float, float]], ffmpeg: str) -> list[Path]:
    """Cut `video` into `out` in one run of ffmpeg, by stream copy, at the first key frame
    after each planned start; each piece's timestamps begin at zero."""
    times = ",".join(f"{start:.3f}" for start, _ in parts[1:])
    _run([ffmpeg, "-loglevel", "error", "-y", "-i", str(video), "-c", "copy", "-f", "segment",
          "-segment_times", times, "-reset_timestamps", "1", str(out / f"%03d{video.suffix}")])
    return sorted(out.iterdir())


def _name(pieces: list[Path], video: Path, folder: Path, ffprobe: str) -> list[Path]:
    """Move the pieces into `folder` under their names, their places in the live read from
    their own lengths."""
    lengths = [_duration(p, ffprobe) for p in pieces]
    parts: list[Path] = []
    start = 0.0
    try:
        for number, (piece, length) in enumerate(zip(pieces, lengths), 1):
            name = part_name(video.stem, video.suffix, number, len(pieces), start, start + length)
            parts.append(piece.replace(folder / name))
            start += length
    except OSError:
        for part in parts:
            part.unlink(missing_ok=True)
        raise CutError(f"{video.name}: could not move the parts into {folder}") from None
    return parts


def _duration(path: Path, ffprobe: str) -> float:
    out = _run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(out.strip())
    except ValueError:
        raise CutError(f"ffprobe gave no length for {path.name}") from None


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8",
                              errors="replace").stdout
    except FileNotFoundError as e:
        raise CutError(f"{cmd[0]} not found") from e
    except subprocess.CalledProcessError as e:
        raise CutError(f"{Path(cmd[0]).name} failed: {(e.stderr or '').strip()[:300]}") from e
