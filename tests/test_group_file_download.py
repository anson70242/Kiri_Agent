"""The download of a group file from QQ's host: at once, streamed to the downloads volume,
its SHA-256 worked out, the size QQ announced checked, and only QQ's https addresses
fetched. Through the group-files module with a fake host."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import pytest

from backend.group_files.jobs import GroupFileJobs

from .group_file_fakes import HOST, make


def test_the_file_is_downloaded_to_the_temporary_folder_with_its_sha256_before_any_upload(tmp_path):
    s = make(tmp_path)
    data = b"x" * 200_000
    s.post("【熟肉】第3话.mp4", data)

    assert asyncio.run(s.keeper.download_next()) is True

    (job,) = GroupFileJobs(s.jobs_file).load()
    assert job.downloaded and job.sha256 == hashlib.sha256(data).hexdigest()
    (local,) = [p for p in s.temp.rglob("*") if p.is_file()]
    assert local.name == "【熟肉】第3话.mp4" and local.read_bytes() == data
    assert s.drive.put == []


def test_a_file_of_the_wrong_size_is_not_uploaded(tmp_path, caplog):
    s = make(tmp_path)
    with caplog.at_level(logging.WARNING, logger="backend.group_files"):
        s.post("【待校】第3话.ass", b"half", size=8)
        s.settle()
    assert s.drive.put == [] and GroupFileJobs(s.jobs_file).load() == []
    assert list(s.temp.rglob("*.*")) == []
    assert any("大小" in r.message for r in caplog.records)


def test_a_host_error_fails_the_job(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", url=f"{HOST}/ftn_handler/gone")
    del s.host.files[f"{HOST}/ftn_handler/gone"]
    s.settle()
    assert s.drive.put == [] and GroupFileJobs(s.jobs_file).load() == []


@pytest.mark.parametrize("url", ["http://grpfile.ftn.qq.com/ftn_handler/x", "https://qq.com.example.net/x",
                                 "https://notqq.com/x", "ftp://grpfile.ftn.qq.com/x", "/ftn_handler/x", ""])
def test_an_address_that_is_not_qqs_is_never_fetched(tmp_path, url, caplog):
    s = make(tmp_path)
    with caplog.at_level(logging.INFO, logger="backend.group_files"):
        s.post("【待校】第3话.ass", url=url)
        s.settle()
    assert s.host.requests == [] and s.drive.put == [] and GroupFileJobs(s.jobs_file).load() == []
    assert not url or all(url not in r.getMessage() for r in caplog.records)  # an address is never logged


def test_a_redirect_is_followed_on_qqs_hosts_only(tmp_path):
    s = make(tmp_path)
    inside = s.host.add(b"subtitle")
    s.host.redirect(f"{HOST}/ftn_handler/moved", "https://other.ftn.qq.com/elsewhere")
    s.host.redirect("https://other.ftn.qq.com/elsewhere", inside)
    s.post("【待校】第3话.ass", b"subtitle", url=f"{HOST}/ftn_handler/moved")
    del s.host.files[f"{HOST}/ftn_handler/moved"]
    s.settle()
    assert [p[2] for p in s.drive.put] == [b"subtitle"]

    s.host.redirect(f"{HOST}/ftn_handler/away", "https://files.example.net/x")
    s.post("【已校】第3话.ass", url=f"{HOST}/ftn_handler/away")
    s.settle()
    assert "https://files.example.net/x" not in s.host.requests and len(s.drive.put) == 1
