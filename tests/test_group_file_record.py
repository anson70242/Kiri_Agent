"""The record of uploads: every group file the module uploads is noted (the group, the place
on the drive, the size, the SHA-256, the time) in a runtime state file beside the jobs file.
Through the group-files module with a fake host, a fake drive and a fake clock."""

from __future__ import annotations

import hashlib

from backend.group_files.record import UPLOADS_FILE, GroupFileUploads, Upload
from backend.state_file import set_incident_hook

from .group_file_fakes import FOLDER, GROUP, make


def test_an_uploaded_file_is_noted_with_its_group_place_size_sha256_and_time(tmp_path):
    s = make(tmp_path)
    s.clock.t = 1_795_040_000.0
    s.post("【待校】第3话.ass", b"the file")
    s.settle()

    assert GroupFileUploads(s.jobs_file.parent / UPLOADS_FILE).load() == [
        Upload(GROUP, f"{FOLDER}/待校/【待校】第3话.ass", 8, hashlib.sha256(b"the file").hexdigest(), 1_795_040_000.0)]


def test_a_file_not_uploaded_is_not_noted(tmp_path):
    s = make(tmp_path)
    s.drive.error = RuntimeError("网盘出错")
    s.post("【待校】第3话.ass")
    s.settle()
    assert GroupFileUploads(s.jobs_file.parent / UPLOADS_FILE).load() == []


def test_the_record_survives_a_restart(tmp_path):
    path = tmp_path / UPLOADS_FILE
    GroupFileUploads(path).note(Upload("G1", "群/待校/a.ass", 3, "ab" * 32, 1.0))
    GroupFileUploads(path).note(Upload("G1", "群/待校/b.ass", 4, "cd" * 32, 2.0))

    record = GroupFileUploads(path)
    assert record.sha256_at("群/待校/a.ass") == "ab" * 32
    assert record.sha256_at("群/待校/b.ass") == "cd" * 32
    assert record.sha256_at("群/待校/c.ass") is None


def test_a_place_noted_again_holds_its_latest_upload(tmp_path):
    record = GroupFileUploads(tmp_path / UPLOADS_FILE)
    record.note(Upload("G1", "群/待校/a.ass", 3, "ab" * 32, 1.0))
    record.note(Upload("G1", "群/待校/a.ass", 5, "ef" * 32, 9.0))

    assert record.sha256_at("群/待校/a.ass") == "ef" * 32
    assert len(record.load()) == 1


def test_a_damaged_record_is_set_aside_and_the_service_goes_on_empty(tmp_path):
    path = tmp_path / UPLOADS_FILE
    path.write_text("{not json", encoding="utf-8")
    incidents = []
    previous = set_incident_hook(incidents.append)
    try:
        assert GroupFileUploads(path).load() == []
    finally:
        set_incident_hook(previous)

    assert len(incidents) == 1 and incidents[0].backup is not None and incidents[0].backup.exists()
