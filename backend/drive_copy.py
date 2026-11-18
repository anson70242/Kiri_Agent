"""The drive copy of an archive: the one zip of it that goes to the cloud drive.

It holds what the dashboard's "全部下载" zip holds (every video file, the chat, both
transcripts as SRT) plus the highlight table as the Excel sheet the dashboard's Excel
download makes; a missing or damaged text artifact is left out, and an archive without a
video has no drive copy. The zip is stored, not compressed, and ZIP64, named after the
archive's stem (`[20261118][主播] 标题.zip`), and built in the downloads root's drive copy
folder (see `Downloads.drive_copies`), never in the archive folder. It lasts as long as
the `with` block that uses it.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.archive import Archive, ArtifactDamaged, ArtifactMissing, Downloads
from backend.monitor.archives import highlights_xlsx_name, stream_zip, zip_contents
from backend.sheet import highlights_xlsx

log = logging.getLogger(__name__)


def drive_copy_name(archive: Archive) -> str | None:
    """The file name of the archive's drive copy, known without building it; None when the
    archive has no video (and so no drive copy)."""
    if archive.video is None or archive.stem is None:
        return None
    return f"{archive.stem}.zip"


@contextmanager
def drive_copy(archive: Archive, root: Path) -> Iterator[Path | None]:
    """The archive's drive copy, built under the downloads `root`: the zip's path, or None
    when the archive has no video. The zip is removed when the block ends, whatever the
    outcome."""
    name = drive_copy_name(archive)
    if name is None:
        yield None
        return
    place = Downloads(root).drive_copies
    place.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=place))  # its own folder: the zip keeps the archive's name
    try:
        files, extra = zip_contents(archive)
        try:
            extra[highlights_xlsx_name(archive)] = highlights_xlsx(archive.read_highlights())
        except (ArtifactMissing, ArtifactDamaged):
            pass
        zip_path = work / name
        with zip_path.open("wb") as out:
            for chunk in stream_zip(files, extra):
                out.write(chunk)
        yield zip_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def remove_leftover_drive_copies(root: Path) -> None:
    """Remove every drive copy zip under the downloads `root`: when the service starts, the
    ones there were left by a kill (their uploads are done again)."""
    place = Downloads(root).drive_copies
    if place.exists():
        shutil.rmtree(place, ignore_errors=True)
        log.info("removed drive copies left over from a kill: %s", place)
