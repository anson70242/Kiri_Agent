"""The Archive module, against a real temporary downloads tree: lookup, presence, listing,
reading, folder references, archive date."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.archive import (
    Archive, ArchiveNotFound, ArtifactDamaged, ArtifactMissing, Artifact, Downloads, file_date)

from .helpers import sample_archive

STEM = "[20261105][柊優花] 歌枠"


def test_an_artifact_is_the_file_named_after_the_video(tmp_path):
    folder = sample_archive(tmp_path)
    a = Archive.from_folder(folder)
    assert a.file(Artifact.VIDEO) == folder / f"{STEM}.mp4"
    assert a.file(Artifact.CHAT) == folder / f"{STEM}.chat_parsed.json"
    assert a.file(Artifact.TRANSCRIPT) == folder / f"{STEM}.transcript.json"
    assert a.file(Artifact.CHINESE_TRANSCRIPT) == folder / f"{STEM}.zh.transcript.json"
    assert a.file(Artifact.HIGHLIGHTS) == folder / f"{STEM}.highlights.json"
    assert a.file(Artifact.RAW_CHAT) is None


def test_without_the_named_file_the_first_in_sorted_order_is_taken(tmp_path):
    folder = sample_archive(tmp_path, chat=None, transcript=None, zh_transcript=None, highlights=None)
    (folder / "b.chat_parsed.json").write_text("[]", encoding="utf-8")
    (folder / "a.chat_parsed.json").write_text("[]", encoding="utf-8")
    (folder / "a.zh.transcript.json").write_text("[]", encoding="utf-8")
    a = Archive.from_folder(folder)
    assert a.file(Artifact.CHAT) == folder / "a.chat_parsed.json"
    assert a.file(Artifact.TRANSCRIPT) is None  # never the Chinese one
    assert a.file(Artifact.CHINESE_TRANSCRIPT) == folder / "a.zh.transcript.json"
    (folder / "z.transcript.json").write_text("[]", encoding="utf-8")
    assert Archive.from_folder(folder).file(Artifact.TRANSCRIPT) == folder / "z.transcript.json"


def test_the_named_file_wins_over_others_with_the_suffix(tmp_path):
    folder = sample_archive(tmp_path)
    (folder / "0 older.transcript.json").write_text("[]", encoding="utf-8")
    assert Archive.from_folder(folder).file(Artifact.TRANSCRIPT) == folder / f"{STEM}.transcript.json"


def test_a_folder_archive_takes_its_largest_mp4_as_the_video(tmp_path):
    folder = sample_archive(tmp_path, video=b"big video")
    (folder / "0 fragment.f137.mp4").write_bytes(b"x")  # sorts first, but is smaller
    a = Archive.from_folder(folder)
    assert a.video == folder / f"{STEM}.mp4" and a.stem == STEM and a.video_size == len(b"big video")
    assert a.files(Artifact.VIDEO) == [folder / "0 fragment.f137.mp4", folder / f"{STEM}.mp4"]


def test_raw_chat_is_either_platforms_file(tmp_path):
    folder = sample_archive(tmp_path, raw_chat={})
    assert Archive.from_folder(folder).file(Artifact.RAW_CHAT) == folder / f"{STEM}.live_chat.json"
    twitch = sample_archive(tmp_path, title="雑談", raw_chat=None)
    (twitch / "[20261105][柊優花] 雑談.chat.json").write_text("[]", encoding="utf-8")
    assert Archive.from_folder(twitch).file(Artifact.RAW_CHAT) == twitch / "[20261105][柊優花] 雑談.chat.json"
    assert Archive.from_folder(twitch).file(Artifact.CHAT) == twitch / "[20261105][柊優花] 雑談.chat_parsed.json"


def test_an_archive_opened_from_its_video_names_artifacts_after_it(tmp_path):
    folder = sample_archive(tmp_path)
    (folder / "0 other.highlights.json").write_text("{}", encoding="utf-8")
    a = Archive.from_video(folder / f"{STEM}.mp4")
    assert a.folder == folder and a.stem == STEM and a.video == folder / f"{STEM}.mp4"
    assert a.file(Artifact.HIGHLIGHTS) == folder / f"{STEM}.highlights.json"


def test_an_archive_opened_from_a_video_of_any_format_names_artifacts_without_its_extension(tmp_path):
    a = Archive.from_video(tmp_path / "x.mkv")
    assert a.stem == "x"
    assert a.save(Artifact.TRANSCRIPT, []) == tmp_path / "x.transcript.json"
    assert Archive.from_video(tmp_path / "[20261105][A] 歌枠 Vol.2.mp4").stem == "[20261105][A] 歌枠 Vol.2"


def test_presence_is_reported_per_artifact(tmp_path):
    folder = sample_archive(tmp_path, zh_transcript=None, highlights=None)
    a = Archive.from_folder(folder)
    assert a.present() == {Artifact.VIDEO, Artifact.CHAT, Artifact.TRANSCRIPT}
    assert a.has(Artifact.TRANSCRIPT) and not a.has(Artifact.CHINESE_TRANSCRIPT)
    assert not a.downloading


def test_downloading_means_no_video_or_a_part_file(tmp_path):
    part = sample_archive(tmp_path, title="录制中")
    (part / "x.f137.mp4.part").write_bytes(b"x")
    assert Archive.from_folder(part).downloading
    chat_only = sample_archive(tmp_path, title="只有弹幕", video=None)
    assert Archive.from_folder(chat_only).downloading
    tmp = sample_archive(tmp_path, title="保存中")
    (tmp / "x.highlights.json.tmp").write_text("{", encoding="utf-8")  # an artifact being saved
    assert not Archive.from_folder(tmp).downloading


def test_reading_returns_parsed_contents(tmp_path):
    a = Archive.from_folder(sample_archive(tmp_path))
    assert [m["msg"] for m in a.read_chat()] == ["hi", "yo", "8888"]
    assert a.read_transcript() == [{"start": 1.0, "end": 2.0, "text": "やっほー"}]
    assert a.read_transcript("zh") == [{"start": 1.0, "end": 2.0, "text": "哈喽"}]
    assert [r["score"] for r in a.read_highlights()["rows"]] == [8, 5]
    assert a.chat_count() == 3


def test_chat_that_is_not_a_list_reads_as_no_messages(tmp_path):
    a = Archive.from_folder(sample_archive(tmp_path, chat={"oops": 1}))
    assert a.read_chat() == [] and a.chat_count() == 0


def test_reading_tells_a_missing_artifact_from_a_damaged_one(tmp_path):
    missing = Archive.from_folder(sample_archive(tmp_path, title="空", chat=None, transcript=None,
                                                 zh_transcript=None, highlights=None))
    damaged = Archive.from_folder(sample_archive(tmp_path, title="坏", chat="[{", transcript="nope",
                                                 zh_transcript=b"\xff\xfe", highlights="{"))
    for read in ("read_chat", "read_transcript", "read_highlights"):
        with pytest.raises(ArtifactMissing):
            getattr(missing, read)()
        with pytest.raises(ArtifactDamaged):
            getattr(damaged, read)()
    with pytest.raises(ArtifactMissing):
        missing.read_transcript("zh")
    with pytest.raises(ArtifactDamaged):
        damaged.read_transcript("zh")
    assert missing.chat_count() == 0 and damaged.chat_count() == 0


def test_the_chat_count_follows_the_file(tmp_path):
    folder = sample_archive(tmp_path)
    assert Archive.from_folder(folder).chat_count() == 3
    chat = folder / f"{STEM}.chat_parsed.json"
    chat.write_text('[{"time": "00:00:01"}]', encoding="utf-8")
    st = chat.stat()
    os.utime(chat, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # a later write, whatever the clock
    assert Archive.from_folder(folder).chat_count() == 1


def test_listing_is_newest_first_by_date_then_modification_time(tmp_path):
    root = tmp_path / "downloads"
    old = sample_archive(root, "A", "20261101", "旧")
    first = sample_archive(root, "B", "20261105", "早")
    second = sample_archive(root, "A", "20261105", "晚")
    t = old.stat().st_mtime
    for i, folder in enumerate((old, first, second)):
        for f in folder.iterdir():
            os.utime(f, (t + i, t + i))
    archives = Downloads(root).list()
    assert [a.folder for a in archives] == [second, first, old]
    a = archives[0]
    assert (a.creator, a.date_folder, a.title, a.ref) == ("A", "20261105", "晚", "A/20261105/晚")
    assert a.mtime == t + 2
    assert a.size == sum(f.stat().st_size for f in second.iterdir())


def test_listing_a_missing_root_is_empty(tmp_path):
    assert Downloads(tmp_path / "nope").list() == []


def test_listing_skips_a_folder_that_vanishes_mid_scan(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    keep = sample_archive(root, "A", "20261105", "留")
    gone = sample_archive(root, "A", "20261106", "删")
    real_scandir = os.scandir

    def racing_scandir(path):  # the folder is deleted (e.g. pruned) between globbing and reading it
        if Path(path) == gone and gone.exists():
            monkeypatch.undo()  # rmtree scans too
            shutil.rmtree(gone)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", racing_scandir)
    assert [a.folder for a in Downloads(root).list()] == [keep]


def test_an_empty_archive_folder_dates_from_the_folder(tmp_path):
    root = tmp_path / "downloads"
    folder = root / "A" / "20261105" / "t"
    folder.mkdir(parents=True)
    [a] = Downloads(root).list()
    assert a.mtime == folder.stat().st_mtime and a.size == 0 and a.downloading


def test_a_folder_reference_opens_the_archive(tmp_path):
    root = tmp_path / "downloads"
    folder = sample_archive(root)
    a = Downloads(root).open("柊優花/20261105/歌枠")
    assert a.folder == folder.resolve() and a.creator == "柊優花" and a.ref == "柊優花/20261105/歌枠"
    assert a.video == folder.resolve() / f"{STEM}.mp4"


@pytest.mark.parametrize("ref", ["", ".", "..", "../", "../..", "柊優花/../..", "nope/20261105/歌枠",
                                 f"柊優花/20261105/歌枠/{STEM}.mp4"])
def test_a_folder_reference_outside_the_root_the_root_itself_or_not_a_folder_is_rejected(tmp_path, ref):
    root = tmp_path / "downloads"
    sample_archive(root)
    with pytest.raises(ArchiveNotFound):
        Downloads(root).open(ref)


def test_a_named_file_is_served_only_from_directly_inside_the_archive(tmp_path):
    root = tmp_path / "downloads"
    folder = sample_archive(root)
    (folder / "sub").mkdir()
    (tmp_path / "secret.txt").write_text("x")
    a = Downloads(root).open("柊優花/20261105/歌枠")
    assert a.member(f"{STEM}.mp4") == folder.resolve() / f"{STEM}.mp4"
    assert a.member("missing.mp4") is None
    assert a.member("sub") is None  # not a plain file
    for bad in ("../../../../secret.txt", "sub/x", "..", ""):
        with pytest.raises(ValueError):
            a.member(bad)


def test_a_files_date_comes_from_the_date_folder_else_its_own_name(tmp_path):
    folder = sample_archive(tmp_path / "downloads", "A", "20261105", "t", stem="[20261231][A] t")
    assert file_date(folder / "[20261231][A] t.highlights.json") == "20261105"
    loose = tmp_path / "copied"  # a table copied out of its archive
    assert file_date(loose / "[20261107][A] t.highlights.json") == "20261107"
    assert file_date(loose / "t.highlights.json") == ""  # nothing dated


def test_the_archive_module_depends_on_no_monitor_downloader_notify_or_pipeline_code():
    code = ("import sys, backend.archive; "
            "print(sorted(m for m in sys.modules if m.startswith('backend.') and m != 'backend.archive'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_an_archive_opened_from_a_video_not_yet_downloaded(tmp_path):
    a = Archive.from_video(tmp_path / "A" / "20261105" / "t" / "[20261105][A] t.mp4")
    assert a.video is None and a.video_size == 0 and a.present() == set() and a.downloading
