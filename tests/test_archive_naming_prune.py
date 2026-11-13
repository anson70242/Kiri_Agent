"""The Archive module, against a real temporary downloads tree: naming a new archive, pruning
old archives, and the folder reference of an archive folder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.archive import Archive, Artifact, Downloads, artifact_name, name_archive, safe_name

from .helpers import sample_archive


# ---- naming a new archive ----

def test_a_new_archive_is_filed_under_creator_date_title_and_named_date_creator_title():
    folder, stem = name_archive("柊優花", "20261105", "歌枠 Vol.2")
    assert folder == Path("柊優花", "20261105", "歌枠 Vol.2")
    assert stem == "[20261105][柊優花] 歌枠 Vol.2"


def test_names_are_made_safe_for_windows_and_linux():
    assert safe_name('a/b:c*?"<>|d') == "a_b_c______d"
    assert safe_name("  many   spaces. ") == "many spaces"
    assert safe_name("...") == "_"
    assert len(safe_name("x" * 500)) == 150
    folder, stem = name_archive("A/B", "20261105", ' 歌枠: "live"  ')
    assert folder == Path("A_B", "20261105", "歌枠_ _live_")
    assert stem == "[20261105][A_B] 歌枠_ _live_"


def test_the_files_of_a_new_archive_are_named_after_its_stem():
    stem = "[20261105][柊優花] 歌枠"
    assert artifact_name(stem, Artifact.VIDEO) == f"{stem}.mp4"
    assert artifact_name(stem, Artifact.RAW_CHAT, "youtube") == f"{stem}.live_chat.json"
    assert artifact_name(stem, Artifact.RAW_CHAT, "twitch") == f"{stem}.chat.json"
    assert artifact_name(stem, Artifact.HIGHLIGHTS) == f"{stem}.highlights.json"


@pytest.mark.parametrize("platform", [None, "", "twitcasting"])
def test_a_raw_chat_cannot_be_named_without_a_known_platform(platform):
    with pytest.raises(ValueError, match="platform"):
        artifact_name("[20261105][柊優花] 歌枠", Artifact.RAW_CHAT, platform)


def test_an_archive_finds_the_files_it_was_named_with(tmp_path):
    folder, stem = name_archive("柊優花", "20261105", "歌枠")
    out = tmp_path / folder
    out.mkdir(parents=True)
    for name in (artifact_name(stem, Artifact.VIDEO), artifact_name(stem, Artifact.RAW_CHAT, "twitch")):
        (out / name).write_bytes(b"x")
    a = Archive.from_video(out / artifact_name(stem, Artifact.VIDEO))
    assert a.present() == {Artifact.VIDEO, Artifact.RAW_CHAT}
    assert a.date_folder == "20261105" and a.creator == "柊優花" and a.title == "歌枠"


# ---- prune ----

def dated_archives(root: Path, creator: str, dates: list[str]) -> list[Path]:
    """One archive folder per date (titles t0, t1, ...), each folder's time one second later."""
    folders = []
    for i, date in enumerate(dates):
        f = sample_archive(root, creator, date, f"t{i}")
        os.utime(f, (1000 + i, 1000 + i))
        folders.append(f)
    return folders


def test_prune_deletes_the_oldest_beyond_the_keep_count_by_date_then_time(tmp_path):
    root = tmp_path / "downloads"
    a = dated_archives(root, "A", ["20261101", "20261102", "20261102", "20261105"])
    (other,) = dated_archives(root, "B", ["20261001"])  # another creator: never counted or touched

    removed = Downloads(root).prune(a[3], keep=2)

    assert removed == [a[0], a[1]]
    assert not a[0].exists() and not a[1].exists()
    assert a[2].exists() and a[3].exists() and other.exists()
    assert not (root / "A" / "20261101").exists()  # the date folder left empty goes too
    assert (root / "A" / "20261102").exists()  # still holds t2


def test_prune_within_one_date_goes_by_folder_time(tmp_path):
    root = tmp_path / "downloads"
    a = dated_archives(root, "A", ["20261102", "20261102", "20261102"])
    os.utime(a[0], (5000, 5000))  # touched last: now the newest of the day
    assert Downloads(root).prune(a[0], keep=1) == [a[1], a[2]]


def test_prune_never_deletes_the_protected_archive(tmp_path):
    root = tmp_path / "downloads"
    new = sample_archive(root, "A", "20260101", "old-date-but-new")
    newer = sample_archive(root, "A", "20261231", "x")
    assert Downloads(root).prune(new, keep=1) == [newer]
    assert new.exists() and not newer.exists()
    assert Downloads(root).prune(new, keep=0) == []


def test_prune_within_the_keep_count_deletes_nothing(tmp_path):
    root = tmp_path / "downloads"
    a = dated_archives(root, "A", ["20261101", "20261102"])
    assert Downloads(root).prune(a[1], keep=2) == []
    assert all(f.exists() for f in a)


def test_prune_refuses_anything_not_directly_under_the_downloads_root(tmp_path):
    root = tmp_path / "downloads"
    sample_archive(root, "A", "20261101", "t")
    stray = sample_archive(tmp_path / "elsewhere", "a", "20261101", "b")
    assert Downloads(root).prune(stray, keep=0) == []
    assert stray.exists()
    deep = sample_archive(root / "A" / "20261101" / "t", "x", "20261102", "y")  # nested a level too deep
    assert Downloads(root).prune(deep, keep=0) == []
    assert deep.exists() and (root / "A" / "20261101" / "t").exists()


# ---- folder reference ----

def test_an_archive_folder_has_the_folder_reference_that_opens_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = sample_archive(Path("downloads"), "A", "20261110", "歌枠 (Vol.2)!")  # a relative path
    downloads = Downloads(Path("downloads"))
    assert downloads.ref(folder) == "A/20261110/歌枠 (Vol.2)!"
    assert downloads.ref(tmp_path / folder) == "A/20261110/歌枠 (Vol.2)!"
    assert downloads.open(downloads.ref(folder)).folder == (tmp_path / folder).resolve()


def test_a_folder_outside_the_downloads_root_has_no_folder_reference(tmp_path):
    with pytest.raises(ValueError):
        Downloads(tmp_path / "downloads").ref(sample_archive(tmp_path / "elsewhere"))
