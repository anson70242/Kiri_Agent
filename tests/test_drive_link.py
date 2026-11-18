"""The drive link of an archive: kept in the archive as an artifact, returned by the
archives API, never in a zip nor in what a finished live is reported to have."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.archive import Archive, Artifact, ArtifactDamaged, DriveLink, artifact_name
from backend.downloader.auto import ready_detail
from backend.drive_copy import drive_copy
from backend.monitor.archives import create_router

from .helpers import sample_archive

FOLDER = "柊優花/20261105/歌枠"
LINK = DriveLink(url="https://pan.example/s/f00dcafe?pwd=k7q2", code="k7q2",
                 path="录播/柊優花/[20261105][柊優花] 歌枠.zip",
                 made=datetime(2026, 11, 18, 12, 30, tzinfo=timezone.utc))


@pytest.fixture
def root(tmp_path):
    return tmp_path / "downloads"


def api_archive(root):
    app = FastAPI()
    app.include_router(create_router(root))
    (archive,) = TestClient(app).get("/api/archives").json()["archives"]
    return archive


def test_a_drive_link_saved_in_the_archive_is_returned_by_the_archives_api(root):
    Archive.from_folder(sample_archive(root)).save(Artifact.DRIVE_LINK, LINK)

    assert api_archive(root)["drive_link"] == {
        "url": "https://pan.example/s/f00dcafe?pwd=k7q2", "code": "k7q2",
        "path": "录播/柊優花/[20261105][柊優花] 歌枠.zip", "made": "2026-11-18T12:30:00+00:00",
    }
    assert Archive.from_folder(root / FOLDER).read_drive_link() == LINK


def test_an_archive_without_a_drive_link_returns_none(root):
    sample_archive(root)
    assert api_archive(root)["drive_link"] is None


@pytest.mark.parametrize("content", [
    "{not json", "[]", '{"url": "https://pan.example/s/f00dcafe?pwd=k7q2"}',
    '{"url": "u", "code": "c", "path": "p", "made": "yesterday"}',
], ids=["not json", "not an object", "fields missing", "bad time"])
def test_a_damaged_drive_link_returns_none(root, content):
    folder = sample_archive(root)
    stem = Archive.from_folder(folder).stem
    (folder / artifact_name(stem, Artifact.DRIVE_LINK)).write_text(content, encoding="utf-8")

    assert api_archive(root)["drive_link"] is None
    with pytest.raises(ArtifactDamaged):
        Archive.from_folder(folder).read_drive_link()


def test_the_drive_link_is_in_neither_zip_nor_in_what_a_finished_live_has(root):
    folder = sample_archive(root)
    archive = Archive.from_folder(folder)
    archive.save(Artifact.DRIVE_LINK, LINK)
    app = FastAPI()
    app.include_router(create_router(root))

    dashboard_zip = zipfile.ZipFile(io.BytesIO(TestClient(app).get("/api/archives/zip", params={"folder": FOLDER}).content))
    with drive_copy(Archive.from_folder(folder), root) as zip_path, zipfile.ZipFile(zip_path) as z:
        drive_copy_names = z.namelist()
    for names in (dashboard_zip.namelist(), drive_copy_names):
        assert not any("drive_link" in n or "pan.example" in n for n in names)
        assert len(names) == len(set(names))
    assert len(dashboard_zip.namelist()) == 4 and len(drive_copy_names) == 5  # as without a drive link
    assert ready_detail(archive.video) == "影片 ✅ · 弹幕 ✅ · 日文字幕 ✅ · 中文字幕 ✅ · 路灯表 ✅"
