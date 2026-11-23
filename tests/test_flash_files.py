"""The files of a flash transfer: what is sent for a made-up archive, the staging folder it
needs, and removing it."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from backend.archive import Archive, Downloads
from backend.drive_copy import drive_copy
from backend.flash.client import FlashFailed
from backend.flash.files import ffmpeg_tools, needs_cutting, prepare, remove_leftover_staging, remove_staging, staging_folder

from .helpers import bare_archive, sample_archive

STEM = "[20261105][森野鈴] 歌枠"


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    return folder


def archive(folder: Path) -> Archive:
    return Archive.from_folder(folder)


def test_a_flash_transfer_holds_the_video_the_chat_both_subtitles_and_the_highlight_table_as_separate_files(root):
    folder = sample_archive(root, raw_chat={"raw": True})

    prepared = prepare(archive(folder), root)

    assert sorted(f.name for f in prepared.files) == sorted([
        f"{STEM}.mp4", f"{STEM}.chat_parsed.json", f"{STEM}.zh.srt", f"{STEM}.ja.srt", f"{STEM}.highlights.xlsx"])
    assert not any(f.suffix == ".zip" for f in prepared.files)


def test_a_flash_transfer_is_named_like_the_archive(root):
    folder = sample_archive(root, "森野鈴", "20261122", "标题")

    assert prepare(archive(folder), root).name == "[20261122][森野鈴] 标题"


def test_it_holds_what_the_drive_copy_holds_byte_for_byte_but_not_zipped(root):
    folder = sample_archive(root)

    prepared = prepare(archive(folder), root)

    with drive_copy(archive(folder), root) as zip_path, zipfile.ZipFile(zip_path) as z:
        zipped = {name: z.read(name) for name in z.namelist()}
    assert {f.name: f.read_bytes() for f in prepared.files} == zipped


def test_a_video_that_fits_and_the_chat_are_sent_from_where_they_are_untouched(root):
    folder = sample_archive(root)

    prepared = prepare(archive(folder), root)

    assert folder / f"{STEM}.mp4" in prepared.files
    assert folder / f"{STEM}.chat_parsed.json" in prepared.files
    assert (folder / f"{STEM}.mp4").read_bytes() == b"0123456789"


def test_the_subtitles_and_the_highlight_table_are_written_into_the_archives_staging_folder(root):
    folder = sample_archive(root)

    prepared = prepare(archive(folder), root)

    staged = [f for f in prepared.files if f.parent != folder]
    assert sorted(f.name for f in staged) == sorted([f"{STEM}.zh.srt", f"{STEM}.ja.srt", f"{STEM}.highlights.xlsx"])
    assert {f.parent for f in staged} == {staging_folder(archive(folder), root)}
    assert "哈喽" in (staging_folder(archive(folder), root) / f"{STEM}.zh.srt").read_text(encoding="utf-8")


def test_the_staging_folder_is_hidden_under_the_downloads_folder_and_never_inside_the_archive(root):
    folder = sample_archive(root)

    staging = staging_folder(archive(folder), root)

    assert staging.is_relative_to(root.resolve())
    assert staging.relative_to(root.resolve()).parts[0].startswith(".")
    assert not staging.is_relative_to(folder)


def test_two_archives_have_two_staging_folders(root):
    one = sample_archive(root, "森野鈴", "20261105", "歌枠")
    other = sample_archive(root, "森野鈴", "20261106", "歌枠")

    assert staging_folder(archive(one), root) != staging_folder(archive(other), root)


def test_a_file_missing_for_a_live_is_left_out(root):
    folder = bare_archive(root, video=b"v", chat=[{"time": "00:00:01", "user": "a", "msg": "hi"}])

    prepared = prepare(archive(folder), root)

    assert [f.name for f in prepared.files] == [f"{STEM}.mp4", f"{STEM}.chat_parsed.json"]


def test_a_damaged_transcript_is_left_out(root):
    folder = sample_archive(root, chinese_transcript="{not json")

    names = [f.name for f in prepare(archive(folder), root).files]

    assert f"{STEM}.zh.srt" not in names and f"{STEM}.ja.srt" in names


def test_an_archive_without_a_video_cannot_be_prepared(root):
    folder = bare_archive(root, chat=[])

    with pytest.raises(FlashFailed):
        prepare(archive(folder), root)


def test_the_listing_of_archives_does_not_look_into_the_staging_folder(root):
    folder = sample_archive(root)
    prepare(archive(folder), root)

    assert [a.folder for a in Downloads(root).list()] == [folder.resolve()]


# ---- removing ----

def test_the_staging_folder_is_removed_when_told(root):
    folder = sample_archive(root)
    prepare(archive(folder), root)

    remove_staging(archive(folder), root)

    assert not staging_folder(archive(folder), root).exists()
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        f"{STEM}{s}" for s in (".mp4", ".chat_parsed.json", ".transcript.json", ".zh.transcript.json", ".highlights.json"))


def test_removing_a_staging_folder_that_is_not_there_does_nothing(root):
    remove_staging(archive(sample_archive(root)), root)


def test_preparing_again_starts_from_an_empty_staging_folder(root):
    folder = sample_archive(root)
    staging = staging_folder(archive(folder), root)
    staging.mkdir(parents=True)
    (staging / "left from an earlier try.mp4").write_bytes(b"old")

    prepared = prepare(archive(folder), root)

    assert not (staging / "left from an earlier try.mp4").exists()
    assert set(staging.iterdir()) == {f for f in prepared.files if f.parent == staging}


def test_every_staging_folder_left_over_is_removed_except_those_named(root):
    kept, gone, other = (sample_archive(root, creator, "20261105", "歌枠") for creator in ("森野鈴", "青葉みのり", "向日葵"))
    for folder in (kept, gone, other):
        prepare(archive(folder), root)

    remove_leftover_staging(root, keep=[kept])

    assert staging_folder(archive(kept), root).is_dir()
    assert any(staging_folder(archive(kept), root).iterdir())
    assert not staging_folder(archive(gone), root).exists()
    assert not staging_folder(archive(other), root).exists()
    hidden = staging_folder(archive(kept), root).relative_to(root.resolve()).parts[0]
    assert sorted(p.name for p in (root / hidden).iterdir()) == ["森野鈴"]


def test_with_nothing_named_every_staging_folder_goes(root):
    folder = sample_archive(root)
    prepare(archive(folder), root)
    hidden = staging_folder(archive(folder), root).relative_to(root.resolve()).parts[0]

    remove_leftover_staging(root)

    assert not (root / hidden).exists()
    assert folder.is_dir()


def test_removing_leftovers_without_any_staging_folder_does_nothing(root):
    remove_leftover_staging(root, keep=[root / "森野鈴" / "20261105" / "歌枠"])


# ---- when a video is cut ----

def test_only_a_video_over_the_limit_needs_cutting(root):
    folder = sample_archive(root, video=b"x" * 100)

    assert not needs_cutting(archive(folder), limit=100)
    assert needs_cutting(archive(folder), limit=99)


def test_an_archive_without_a_video_needs_no_cutting(root):
    assert not needs_cutting(archive(bare_archive(root, chat=[])))


def test_a_cutting_that_fails_is_a_failure_and_leaves_no_staging_folder(root, tmp_path):
    folder = sample_archive(root, video=b"x" * 100)

    with pytest.raises(FlashFailed):
        prepare(archive(folder), root, ffmpeg=str(tmp_path / "no-ffmpeg-here" / "ffmpeg"), limit=10)

    assert not staging_folder(archive(folder), root).exists()
    assert (folder / f"{STEM}.mp4").read_bytes() == b"x" * 100


# ---- ffmpeg as the downloader's settings name it ----

def test_without_a_setting_ffmpeg_and_ffprobe_are_found_on_the_path():
    assert ffmpeg_tools(None) == ("ffmpeg", "ffprobe")
    assert ffmpeg_tools("") == ("ffmpeg", "ffprobe")


def test_a_folder_setting_holds_both(tmp_path):
    assert ffmpeg_tools(str(tmp_path)) == (str(tmp_path / "ffmpeg"), str(tmp_path / "ffprobe"))


def test_a_binary_setting_has_ffprobe_beside_it(tmp_path):
    binary = tmp_path / "ffmpeg.exe"
    binary.write_bytes(b"")

    assert ffmpeg_tools(str(binary)) == (str(binary), str(tmp_path / "ffprobe.exe"))
