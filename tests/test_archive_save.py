"""Saving artifacts through the Archive module, against a real temporary directory: the
final name, the bytes on disk, and the temporary file of an unfinished save."""

from __future__ import annotations

import json

import pytest

from backend.archive import Archive, Artifact

from .helpers import sample_archive

STEM = "[20261105][柊優花] 歌枠"
SEGMENTS = [{"start": 0.78, "end": 3.78, "text": "めっちゃ味するよ!"}, {"start": 4.0, "end": 5.5, "text": "哈喽"}]


def test_a_saved_artifact_appears_under_its_final_name_with_no_temporary_file_left(tmp_path):
    folder = sample_archive(tmp_path, transcript=None)
    a = Archive.from_folder(folder)
    out = a.save(Artifact.TRANSCRIPT, SEGMENTS)
    assert out == folder / f"{STEM}.transcript.json"
    assert a.has(Artifact.TRANSCRIPT) and a.read_transcript() == SEGMENTS
    assert sorted(p.name for p in folder.iterdir()) == sorted([
        f"{STEM}.mp4", f"{STEM}.chat_parsed.json", f"{STEM}.transcript.json",
        f"{STEM}.zh.transcript.json", f"{STEM}.highlights.json"])


def test_a_save_that_cannot_finish_leaves_a_tmp_file_never_taken_for_a_download(tmp_path):
    folder = sample_archive(tmp_path, transcript=None)
    (folder / f"{STEM}.transcript.json").mkdir()  # the final name is taken: the rename fails
    a = Archive.from_folder(folder)
    with pytest.raises(OSError):
        a.save(Artifact.TRANSCRIPT, SEGMENTS)
    assert (folder / f"{STEM}.transcript.json.tmp").is_file()
    assert not any(p.name.endswith(".part") for p in folder.iterdir())
    assert not Archive.from_folder(folder).downloading


def test_highlights_are_saved_with_one_space_indent(tmp_path):
    folder = sample_archive(tmp_path, highlights=None)
    table = {"streamer": "柊優花", "rows": [{"start": "00:00:00", "score": 7}]}
    out = Archive.from_folder(folder).save(Artifact.HIGHLIGHTS, table)
    assert out.read_text(encoding="utf-8") == (
        '{\n "streamer": "柊優花",\n "rows": [\n  {\n   "start": "00:00:00",\n   "score": 7\n  }\n ]\n}'
    )


def test_lists_are_saved_one_compact_item_per_line(tmp_path):
    folder = sample_archive(tmp_path, chat=None, zh_transcript=None)
    a = Archive.from_folder(folder)
    out = a.save(Artifact.CHINESE_TRANSCRIPT, SEGMENTS)
    assert out.read_text(encoding="utf-8") == (
        '[\n'
        '  {"start": 0.78, "end": 3.78, "text": "めっちゃ味するよ!"},\n'
        '  {"start": 4.0, "end": 5.5, "text": "哈喽"}\n'
        ']\n'
    )
    assert a.save(Artifact.CHAT, []).read_text(encoding="utf-8") == "[]\n"
    assert a.file(Artifact.CHAT) == folder / f"{STEM}.chat_parsed.json"


def test_a_save_is_named_after_the_video_even_when_another_file_has_the_suffix(tmp_path):
    folder = sample_archive(tmp_path, transcript=None)
    (folder / "0 older.transcript.json").write_text("[]", encoding="utf-8")
    assert Archive.from_folder(folder).save(Artifact.TRANSCRIPT, SEGMENTS) == folder / f"{STEM}.transcript.json"
    assert json.loads((folder / "0 older.transcript.json").read_text(encoding="utf-8")) == []


def test_downloads_are_not_saved_through_the_archive(tmp_path):
    a = Archive.from_folder(sample_archive(tmp_path))
    for artifact in (Artifact.VIDEO, Artifact.RAW_CHAT):
        with pytest.raises(ValueError):
            a.save(artifact, [])


def test_an_archive_opened_from_its_raw_chat_names_artifacts_after_it(tmp_path):
    for raw_name in (f"{STEM}.live_chat.json", f"{STEM}.chat.json"):
        a = Archive.from_file(tmp_path / raw_name)
        assert a.save(Artifact.CHAT, []) == tmp_path / f"{STEM}.chat_parsed.json"
    assert Archive.from_file(tmp_path / f"{STEM}.zh.transcript.json").stem == STEM
    with pytest.raises(ValueError):
        Archive.from_file(tmp_path / "x.json")
