"""The drive copy of an archive: the zip that goes to the cloud drive, built in a temporary
place on the downloads volume and gone after use."""

from __future__ import annotations

import contextlib
import io
import zipfile

import openpyxl
import pytest

from backend.archive import Archive, Downloads
from backend.drive_copy import drive_copy

from .helpers import sample_archive
from .test_drive_restart import run_until
from .test_drive_stage import FakeDrive, make

STEM = "[20261105][森野鈴] 歌枠"


def test_a_full_archive_gives_one_zip_named_after_its_stem_with_video_chat_subtitles_and_sheet(tmp_path):
    root = tmp_path / "downloads"
    folder = sample_archive(root, raw_chat={})

    with drive_copy(Archive.from_folder(folder), root) as zip_path:
        assert zip_path.name == f"{STEM}.zip"
        with zipfile.ZipFile(zip_path) as z:
            assert sorted(z.namelist()) == sorted([
                f"{STEM}.mp4", f"{STEM}.chat_parsed.json", f"{STEM}.zh.srt", f"{STEM}.ja.srt",
                f"{STEM}.highlights.xlsx",
            ])
            assert z.read(f"{STEM}.mp4") == b"0123456789"
            assert "やっほー" in z.read(f"{STEM}.ja.srt").decode()
            assert "哈喽" in z.read(f"{STEM}.zh.srt").decode()
            sheet = openpyxl.load_workbook(io.BytesIO(z.read(f"{STEM}.highlights.xlsx"))).active
            rows = [[c.value for c in row] for row in sheet.iter_rows()]
            assert rows[0][:2] == ["开始", "结束"]  # the Excel download's sheet
            assert rows[1][:2] == ["00:00:00", "00:05:00"] and rows[1][4] == "🏆 开场"
            for info in z.infolist():
                assert info.compress_type == zipfile.ZIP_STORED
            video = z.getinfo(f"{STEM}.mp4")
        # ZIP64: the video's local header carries the ZIP64 extra field (id 0x0001), so a
        # video over 4 GB fits.
        raw = zip_path.read_bytes()
        name = video.filename.encode()
        header = raw[video.header_offset:video.header_offset + 30 + len(name) + 4]
        assert header[30 + len(name):][:2] == b"\x01\x00"


def test_a_missing_or_damaged_text_artifact_is_left_out(tmp_path):
    root = tmp_path / "downloads"
    folder = sample_archive(root, chinese_transcript=None, highlights="{not json")

    with drive_copy(Archive.from_folder(folder), root) as zip_path:
        with zipfile.ZipFile(zip_path) as z:
            assert sorted(z.namelist()) == sorted([f"{STEM}.mp4", f"{STEM}.chat_parsed.json", f"{STEM}.ja.srt"])


def test_an_archive_without_video_has_no_drive_copy(tmp_path):
    root = tmp_path / "downloads"
    folder = sample_archive(root, video=None)

    with drive_copy(Archive.from_folder(folder), root) as zip_path:
        assert zip_path is None
    assert not list(root.rglob("*.zip"))


def test_the_zip_is_built_on_the_downloads_volume_outside_the_archive_and_is_listed_as_no_archive(tmp_path):
    root = tmp_path / "downloads"
    folder = sample_archive(root)

    archive_files = sorted(folder.iterdir())

    with drive_copy(Archive.from_folder(folder), root) as zip_path:
        assert zip_path.is_file()
        assert zip_path.resolve().is_relative_to(root.resolve())
        assert sorted(folder.iterdir()) == archive_files  # nothing written into the archive
        assert [a.folder for a in Downloads(root).list()] == [folder.resolve()]


@pytest.mark.parametrize("fails", [False, True], ids=["uploaded", "failed"])
def test_the_zip_is_gone_after_use_whatever_the_outcome(tmp_path, fails):
    root = tmp_path / "downloads"
    folder = sample_archive(root)
    archive_files = sorted(folder.iterdir())

    with pytest.raises(RuntimeError) if fails else contextlib.nullcontext():
        with drive_copy(Archive.from_folder(folder), root) as zip_path:
            if fails:
                raise RuntimeError("upload failed")
    assert not zip_path.exists()
    assert not list(root.rglob("*.zip"))
    assert sorted(folder.iterdir()) == archive_files


def test_zips_left_over_from_a_kill_are_removed_when_the_service_starts(tmp_path):
    ad, _, _ = make(tmp_path, FakeDrive())
    root = ad.settings.output_dir
    folder = sample_archive(root)
    with drive_copy(Archive.from_folder(folder), root) as zip_path:
        leftover = zip_path.read_bytes()  # a kill would have left it where it was
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(leftover)

    run_until(ad, lambda: not zip_path.exists())
    assert not list(root.rglob("*.zip"))
    assert Archive.from_folder(folder).video is not None  # the archive itself is untouched
