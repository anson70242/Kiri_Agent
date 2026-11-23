"""A flash transfer of an archive whose video is too big for one file, with a real small video
made by ffmpeg and the size limit lowered (skipped when ffmpeg is absent)."""

from __future__ import annotations

import shutil

import pytest

from backend.archive import Archive, Downloads
from backend.flash.files import needs_cutting, prepare, remove_staging, staging_folder

from .helpers import sample_archive
from .test_flash_cut_video import make_video

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg not installed")

STEM = "[20261105][森野鈴] 歌枠"


def test_a_video_over_the_limit_goes_as_parts_written_into_the_staging_folder_and_the_video_stays(tmp_path):
    root = tmp_path / "downloads"
    folder = sample_archive(root, video=None)
    video = make_video(folder / f"{STEM}.mp4")
    original = video.read_bytes()
    size = len(original)
    archive = Archive.from_folder(folder)

    assert needs_cutting(archive, limit=size // 2)
    prepared = prepare(archive, root, limit=size // 2, cutting_size=size * 2 // 5)

    staging = staging_folder(archive, root)
    parts = [f for f in prepared.files if f.suffix == ".mp4"]
    assert video not in prepared.files
    assert [p.name.split(" (")[-1][:3] for p in parts] == ["1／3", "2／3", "3／3"]
    assert {p.parent for p in parts} == {staging}
    assert prepared.files[:3] == parts  # the videos first
    assert video.read_bytes() == original
    assert [a.folder for a in Downloads(root).list()] == [folder.resolve()]

    remove_staging(archive, root)

    assert not staging.exists()
    assert not Downloads(root).flash_staging.exists()
