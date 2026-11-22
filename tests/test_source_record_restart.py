"""The source record sheet across a restart: a row told is saved as a job before the telling
returns, in a state file beside the QQ groups file, and a service started again on the same
files writes the rows that were waiting. Both of the module's state files are runtime state.
Against a fake Tencent Docs endpoint and a real groups store; made-up ids and links."""

from __future__ import annotations

import asyncio

import pytest

from backend.monitor import Streamer
from backend.notify.qq import QQGroups, SourceRecordSheet
from backend.state_file import set_incident_hook

from .source_record_fakes import FILE_ID, TAB_ID, FakeTencentDocs, make, record_sheet, settle
from .test_source_record_processed import processed, store
from .test_source_record_wiring import app

SHEET = SourceRecordSheet(FILE_ID, TAB_ID)
RECORDING = ("G1", {"name": "章鱼烧字幕组", "streamers": ["A"], "source_record_sheet": SHEET})


def restarted(s, tmp_path):
    """The service started again on the same files: a new keeper, a new groups store."""
    return make(s.docs, groups=QQGroups(tmp_path / "qq_groups.json"))


def test_a_row_waiting_when_the_service_stops_is_written_after_the_restart(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)  # told, not written yet

    again = restarted(s, tmp_path)
    again.settle()

    assert again.tab.texts(2)[:3] == ["A", "2026/11/22", "录播"]
    assert again.tab.text(3, 0) == "主播1"


def test_the_state_files_sit_beside_the_qq_groups_file(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    assert (tmp_path / "source_record_jobs.json").exists()
    s.settle()
    assert (tmp_path / "source_record_rows.json").exists()


def test_rows_waiting_are_written_after_the_restart_in_the_order_told(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"streamers": ["A", "B"], "source_record_sheet": SHEET})))
    processed(s.keeper, stream_id="a1")
    processed(s.keeper, Streamer("B", "youtube", "@b"), stream_id="b1")

    again = restarted(s, tmp_path)
    again.settle()
    assert [again.tab.text(r, 0) for r in (2, 3, 4)] == ["B", "A", "主播1"]


@pytest.mark.parametrize("name", ["source_record_jobs.json", "source_record_rows.json"])
def test_a_damaged_state_file_is_set_aside_the_owner_alerted_and_the_service_goes_on_empty(tmp_path, name):
    groups = store(tmp_path, RECORDING)
    (tmp_path / name).write_text("{oops", encoding="utf-8")
    alerts: list = []
    set_incident_hook(alerts.append)
    try:
        s = make(groups=groups)
        processed(s.keeper)
        s.settle()
    finally:
        set_incident_hook(None)
    assert [a.path.name for a in alerts] == [name]
    assert len(list(tmp_path.glob(f"{name}.corrupt-*"))) == 1
    assert s.tab.text(2, 0) == "A"  # the service went on


def test_the_service_started_again_on_the_same_files_writes_the_rows_that_were_waiting(tmp_path, monkeypatch):
    first = app(tmp_path, monkeypatch)
    groups = first.state.notifier.qq.groups
    groups.register("G1")
    groups.update("G1", streamers=["A"], source_record_sheet=SHEET)
    processed(first.state.source_record)  # told, then the service stops before writing it
    first.state.source_record.close()

    again = app(tmp_path, monkeypatch)
    docs = FakeTencentDocs()
    tab = record_sheet(docs)
    again.state.source_record.client = docs.client()
    asyncio.run(settle(again.state.source_record))
    assert tab.texts(2)[:3] == ["A", "2026/11/22", "录播"]
