"""The flash link of an archive: kept in the archive as an artifact, returned by the
archives API until it expires, never in a zip, a drive copy or a flash transfer."""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.archive import Archive, Artifact, ArtifactDamaged, Downloads, FlashLink, artifact_name
from backend.downloader.auto import ready_detail
from backend.drive_copy import drive_copy
from backend.monitor.archives import create_router, zip_contents

from .helpers import sample_archive

FOLDER = "森野鈴/20261105/歌枠"
URL = "https://flash.example/q/MadeUp7f3a"
MADE = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)
LINK = FlashLink(url=URL, expires=MADE + timedelta(days=14), made=MADE)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "downloads"


def api_archive(root, now: datetime = MADE + timedelta(days=1)):
    app = FastAPI()
    app.include_router(create_router(root, now=lambda: now))
    (archive,) = TestClient(app).get("/api/archives").json()["archives"]
    return archive


def test_a_flash_link_saved_in_the_archive_is_read_back_and_returned_by_the_archives_api(root):
    Archive.from_folder(sample_archive(root)).save(Artifact.FLASH_LINK, LINK)

    assert Archive.from_folder(root / FOLDER).read_flash_link() == LINK
    assert api_archive(root)["flash_link"] == {
        "url": URL, "expires": "2026-12-07T12:30:00+00:00", "made": "2026-11-23T12:30:00+00:00",
    }


@pytest.mark.parametrize("now, shown", [
    (MADE + timedelta(days=14, seconds=-1), True),
    (MADE + timedelta(days=14), False),
    (MADE + timedelta(days=30), False),
], ids=["a second before it expires", "when it expires", "long after"])
def test_the_archives_api_gives_the_flash_link_only_until_it_expires(root, now, shown):
    Archive.from_folder(sample_archive(root)).save(Artifact.FLASH_LINK, LINK)

    assert (api_archive(root, now)["flash_link"] is not None) is shown
    assert Archive.from_folder(root / FOLDER).read_flash_link() == LINK  # the file stays


def test_an_archive_without_a_flash_link_returns_none(root):
    sample_archive(root)
    assert api_archive(root)["flash_link"] is None


@pytest.mark.parametrize("content", [
    "{not json", "[]", f'{{"url": "{URL}"}}',
    f'{{"url": "{URL}", "expires": "in two weeks", "made": "2026-11-23T12:30:00+00:00"}}',
    f'{{"url": "{URL}", "expires": "2026-12-07T12:30:00", "made": "2026-11-23T12:30:00"}}',
], ids=["not json", "not an object", "fields missing", "bad time", "no time zone"])
def test_a_damaged_flash_link_returns_none_and_its_error_does_not_hold_the_address(root, content):
    folder = sample_archive(root)
    stem = Archive.from_folder(folder).stem
    (folder / artifact_name(stem, Artifact.FLASH_LINK)).write_text(content, encoding="utf-8")

    assert api_archive(root)["flash_link"] is None
    with pytest.raises(ArtifactDamaged) as damaged:
        Archive.from_folder(folder).read_flash_link()
    assert "flash.example" not in str(damaged.value)


def test_the_flash_link_is_in_neither_the_zip_nor_the_drive_copy_nor_what_a_flash_transfer_takes(root):
    folder = sample_archive(root)
    archive = Archive.from_folder(folder)
    archive.save(Artifact.FLASH_LINK, LINK)
    app = FastAPI()
    app.include_router(create_router(root))

    dashboard_zip = zipfile.ZipFile(io.BytesIO(TestClient(app).get("/api/archives/zip", params={"folder": FOLDER}).content))
    with drive_copy(Archive.from_folder(folder), root) as zip_path, zipfile.ZipFile(zip_path) as z:
        drive_copy_names = z.namelist()
    files, extra = zip_contents(Archive.from_folder(folder))  # what a flash transfer's files are chosen from
    shared_names = [f.name for f in files] + list(extra)
    for names in (dashboard_zip.namelist(), drive_copy_names, shared_names):
        assert not any("flash_link" in n for n in names)
    assert len(dashboard_zip.namelist()) == len(shared_names) == 4 and len(drive_copy_names) == 5  # as without one
    assert ready_detail(archive.video) == "影片 ✅ · 弹幕 ✅ · 日文字幕 ✅ · 中文字幕 ✅ · 路灯表 ✅"


def test_an_archive_with_a_flash_link_is_listed_and_pruned_with_its_link(root):
    old = sample_archive(root, date="20261101", title="雑談")
    Archive.from_folder(old).save(Artifact.FLASH_LINK, LINK)
    new = sample_archive(root)

    assert [a.ref for a in Downloads(root).list()] == [FOLDER, "森野鈴/20261101/雑談"]
    assert Downloads(root).prune(new, keep=1) == [old]
    assert not old.exists() and not old.parent.exists()


def test_the_flash_link_is_in_no_log_line(root, caplog):
    caplog.set_level(logging.DEBUG)
    old = sample_archive(root, date="20261101", title="雑談")
    Archive.from_folder(old).save(Artifact.FLASH_LINK, LINK)
    new = sample_archive(root)
    Archive.from_folder(new).save(Artifact.FLASH_LINK, LINK)

    app = FastAPI()
    app.include_router(create_router(root, now=lambda: MADE))
    TestClient(app).get("/api/archives")
    Downloads(root).prune(new, keep=1)

    assert caplog.records  # the prune was logged
    assert "flash.example" not in caplog.text
