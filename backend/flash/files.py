"""The files of a flash transfer: what an archive's flash transfer sends, and the staging
folder the files no archive holds are written into.

    from backend.flash.files import needs_cutting, note_flash_link, prepare, remove_staging

    if needs_cutting(archive):       # preparing will run ffmpeg (minutes): mark the job busy
        ...
    prepared = prepare(archive, downloads_root, ffmpeg=settings.ffmpeg)
    started = client.start(prepared.files, prepared.name)
    ...                              # client.progress(started.id) until finished
    note_flash_link(archive, started)
    remove_staging(archive, downloads_root)   # once uploaded, or given up on

A flash transfer holds what a drive copy holds (`backend.monitor.archives.full_contents`),
as separate files, nothing zipped: the video (or its parts when it is too big for one file),
the cleaned chat, both transcripts as SRT and the highlight table as Excel. The video and the
chat are sent from where they are; the subtitles, the sheet and the parts are written into
the archive's staging folder (`Downloads.flash_staging`), which the flash service sees on the
same downloads volume.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from backend.archive import Archive, Artifact, Downloads, FlashLink
from backend.monitor.archives import full_contents

from .client import FlashFailed, StartedTransfer
from .cut import CUTTING_SIZE, LIMIT, CutError, cut

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransferFiles:
    """What one flash transfer sends: its name (the archive's stem, `[20261122][森野鈴] 标题`)
    and its files, the videos first."""

    name: str
    files: list[Path]


def staging_folder(archive: Archive, root: Path) -> Path:
    """The archive's staging folder under the downloads `root`. Raises ValueError for an
    archive outside `root`."""
    downloads = Downloads(root)
    return downloads.flash_staging / downloads.ref(archive.folder)


def needs_cutting(archive: Archive, limit: int = LIMIT) -> bool:
    """Whether preparing the archive's flash transfer cuts a video (one over `limit`): the
    only part of a flash transfer that takes this service minutes of work."""
    try:
        return any(video.stat().st_size > limit for video in archive.files(Artifact.VIDEO))
    except OSError:
        return False


def prepare(archive: Archive, root: Path, *, ffmpeg: str | None = None, limit: int = LIMIT,
            cutting_size: int = CUTTING_SIZE) -> TransferFiles:
    """The files of the archive's flash transfer, written where needed into its staging
    folder (emptied first: what an earlier try left goes). `ffmpeg`: the downloader's setting
    (`download.ffmpeg`: the binary, its folder, or None for the PATH). Raises FlashFailed,
    leaving no staging folder, when the archive has no video or the files cannot be made."""
    if archive.video is None or archive.stem is None:
        raise FlashFailed("录播里没有影片")
    try:
        staging = staging_folder(archive, root)
    except ValueError:
        raise FlashFailed("录播不在下载目录里，闪传服务看不到") from None
    shutil.rmtree(staging, ignore_errors=True)
    ffmpeg_binary, ffprobe = ffmpeg_tools(ffmpeg)
    try:
        files, extra = full_contents(archive)
        videos = [f for f in files if Artifact.VIDEO.matches(f.name)]
        sent: list[Path] = []
        for video in videos:
            sent += cut(video, staging, limit=limit, cutting_size=cutting_size, ffmpeg=ffmpeg_binary, ffprobe=ffprobe)
        sent += [f for f in files if f not in videos]
        if extra:
            staging.mkdir(parents=True, exist_ok=True)
        for name, data in extra.items():
            (staging / name).write_bytes(data)
            sent.append(staging / name)
    except (CutError, OSError) as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise FlashFailed(f"准备闪传的文件失败：{e}") from None
    return TransferFiles(archive.stem, sent)


def note_flash_link(archive: Archive, started: StartedTransfer) -> None:
    """Note the started flash transfer's flash link in the archive (once it is uploaded), so
    that the archive's page shows it until it expires."""
    archive.save(Artifact.FLASH_LINK, FlashLink(started.link, started.expires, started.made))


def remove_staging(archive: Archive, root: Path) -> None:
    """Remove the archive's staging folder (its flash transfer is uploaded or given up on);
    nothing happens when there is none."""
    try:
        staging = staging_folder(archive, root)
    except ValueError:
        return
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
        _remove_empty_parents(staging, Downloads(root).flash_staging)


def remove_leftover_staging(root: Path, keep: Iterable[Path] = ()) -> None:
    """Remove every staging folder under the downloads `root` but those of the archive
    folders in `keep` (a flash transfer still uploading): when the service starts, the
    others were left by a kill."""
    downloads = Downloads(root)
    place = downloads.flash_staging
    if not place.exists():
        return
    kept: set[str] = set()
    for folder in keep:
        try:
            kept.add(downloads.ref(folder))
        except ValueError:
            continue
    if kept:
        _remove_all_but(place, place, kept)
    else:
        shutil.rmtree(place, ignore_errors=True)
    log.info("removed flash transfer files left over from a kill, kept %d staging folder(s)", len(kept))


def _remove_all_but(folder: Path, place: Path, kept: set[str]) -> None:
    """Remove everything in `folder` but the staging folders `kept` (references relative to
    `place`) and the folders that lead to them; a folder left empty goes too."""
    for entry in list(folder.iterdir()):
        ref = entry.relative_to(place).as_posix()
        if ref in kept:
            continue
        if entry.is_dir() and any(k.startswith(ref + "/") for k in kept):
            _remove_all_but(entry, place, kept)
            if not any(entry.iterdir()):
                entry.rmdir()
        elif entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def _remove_empty_parents(folder: Path, place: Path) -> None:
    """Remove the folders between `folder` and `place` (`place` included) that are empty."""
    for parent in [*folder.parents]:
        if not parent.is_relative_to(place):
            break
        try:
            parent.rmdir()
        except OSError:  # not empty, or already gone
            break


def ffmpeg_tools(setting: str | None) -> tuple[str, str]:
    """ffmpeg and ffprobe as the downloader's `download.ffmpeg` setting names them: empty for
    the PATH, a folder that holds both, or the ffmpeg binary with ffprobe beside it."""
    if not setting:
        return "ffmpeg", "ffprobe"
    path = Path(setting)
    if path.is_dir():
        return str(path / "ffmpeg"), str(path / "ffprobe")
    if "ffmpeg" not in path.name:
        return setting, "ffprobe"
    return setting, str(path.with_name(path.name.replace("ffmpeg", "ffprobe")))
