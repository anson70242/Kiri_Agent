"""The group-files module's snapshot rows for the queue box: a job waiting for a retry shows
its last error and when it is tried next, like a live's rows do."""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from backend.group_files import GroupFileJob

from .group_file_fakes import make

FIELDS = {f.name for f in dataclasses.fields(GroupFileJob)}


def test_a_fresh_waiting_job_has_no_error_and_is_tried_at_once(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第1话.ass")

    (row,) = s.keeper.snapshot()["queue"]

    assert (row["last_error"], row["next_try"]) == ("", 0)


@pytest.mark.skipif("last_error" not in FIELDS, reason="a job keeps its last error from ticket 07 on")
def test_a_job_waiting_for_a_retry_shows_its_last_error(tmp_path):
    s = make(tmp_path)
    url = s.post("【待校】第1话.ass")
    s.host.answers[url] = httpx.Response(503)
    asyncio.run(s.keeper.download_next())

    (row,) = s.keeper.snapshot()["queue"]

    assert row["waiting_for"] == "download"
    assert row["last_error"]
