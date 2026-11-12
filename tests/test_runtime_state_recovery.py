"""Runtime state other than the download queue: a damaged file is kept as a backup, the
incident is reported, and the store carries on empty. A missing file still means
"nothing yet"."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.downloader.base import VideoInfo
from backend.downloader.pending import PendingChats
from backend.monitor.server import create_app
from backend.notify import LiveNotifier, Notifier
from backend.notify.history import NotifyHistory
from backend.state_file import set_incident_hook

from .helpers import OFFLINE, AlertRecorder, Recorder, ev, live, off

DAMAGED = "[{\"info\": "


@pytest.fixture
def incidents():
    got = []
    previous = set_incident_hook(got.append)
    yield got
    set_incident_hook(previous)


def backups(folder, name: str) -> list:
    return sorted(p for p in folder.iterdir() if p.name.startswith(f"{name}.corrupt-"))


INFO = VideoInfo("fake", "1", "fake://x", "T", "c", "C", "20261110")


# ---- chat retry queue ----

def test_a_damaged_chat_retry_queue_is_backed_up_reported_and_starts_empty(tmp_path, incidents):
    f = tmp_path / "pending.json"
    f.write_text(DAMAGED, encoding="utf-8")
    q = PendingChats(f)

    assert q.load() == []
    (backup,) = backups(tmp_path, "pending.json")
    assert backup.read_text(encoding="utf-8") == DAMAGED
    assert [i.message for i in incidents] == [f"pending.json 损坏，已备份为 {backup.name}，已按空文件继续运行"]

    q.add(INFO, tmp_path / "out", "not yet")  # carries on: the queue works again
    assert [e.info.id for e in PendingChats(f).load()] == ["1"]


def test_a_chat_retry_queue_in_the_wrong_shape_is_damaged(tmp_path, incidents):
    f = tmp_path / "pending.json"
    f.write_text('{"not": "a list"}', encoding="utf-8")

    assert PendingChats(f).load() == []
    assert backups(tmp_path, "pending.json") and len(incidents) == 1


def test_a_missing_chat_retry_queue_is_empty_without_an_incident(tmp_path, incidents):
    q = PendingChats(tmp_path / "pending.json")
    assert q.load() == [] and q.next_due() is None
    assert incidents == [] and list(tmp_path.iterdir()) == []


# ---- the notifier's live-stream state ----

async def one_run(state_file, steps):
    """One monitor run on `state_file`: the events it sends."""
    rec = Recorder()
    n = LiveNotifier([rec], offline_grace=0.01, state_file=state_file)
    for new, old in steps:
        await n.on_change(new, old)
    await asyncio.sleep(0.05)
    await n.drain()
    await n.aclose()
    return rec.events


def test_a_damaged_live_stream_state_is_backed_up_reported_and_starts_fresh(tmp_path, incidents):
    f = tmp_path / "notify_state.json"
    f.write_text('{"version": 1, "streamers": {', encoding="utf-8")

    events = asyncio.run(one_run(f, [(live(), None)]))

    assert events == []  # a fresh start: whoever is live now is not announced
    (backup,) = backups(tmp_path, "notify_state.json")
    assert backup.read_text(encoding="utf-8") == '{"version": 1, "streamers": {'
    assert [i.message for i in incidents] == [f"notify_state.json 损坏，已备份为 {backup.name}，已按空文件继续运行"]
    assert json.loads(f.read_text(encoding="utf-8"))["streamers"]["youtube:@a"]["stream_id"] == "v1"


@pytest.mark.parametrize("content", ['["a list"]', '{"streamers": ["a list"]}',
                                     '{"version": 1, "streamers": {"youtube:@a": {"no": "status"}}}'])
def test_a_live_stream_state_in_the_wrong_shape_is_damaged(tmp_path, incidents, content):
    f = tmp_path / "notify_state.json"
    f.write_text(content, encoding="utf-8")

    assert asyncio.run(one_run(f, [(live(), None)])) == []
    assert backups(tmp_path, "notify_state.json") and len(incidents) == 1


def test_a_missing_live_stream_state_is_a_fresh_start_without_an_incident(tmp_path, incidents):
    f = tmp_path / "notify_state.json"
    assert asyncio.run(one_run(f, [(live(), None)])) == []
    assert asyncio.run(one_run(f, [(off(), None)])) == [ev(OFFLINE)]  # then saved and read back
    assert incidents == [] and backups(tmp_path, "notify_state.json") == []


# ---- notification history ----

def test_a_damaged_notification_history_is_backed_up_reported_and_starts_empty(tmp_path, incidents):
    f = tmp_path / "notify_log.json"
    f.write_text('[{"at": ', encoding="utf-8")

    history = NotifyHistory(f)

    assert history.entries() == []
    (backup,) = backups(tmp_path, "notify_log.json")
    assert backup.read_text(encoding="utf-8") == '[{"at": '
    assert [i.message for i in incidents] == [f"notify_log.json 损坏，已备份为 {backup.name}，已按空文件继续运行"]

    history.add("discord", "discord", "live: A", True)  # carries on, and keeps it on disk
    assert [e["summary"] for e in NotifyHistory(f).entries()] == ["live: A"]


@pytest.mark.parametrize("content", ['{"not": "a list"}', '["not an entry"]'])
def test_a_notification_history_in_the_wrong_shape_is_damaged(tmp_path, incidents, content):
    f = tmp_path / "notify_log.json"
    f.write_text(content, encoding="utf-8")

    assert NotifyHistory(f).entries() == []
    assert backups(tmp_path, "notify_log.json") and len(incidents) == 1


def test_a_missing_notification_history_is_empty_without_an_incident(tmp_path, incidents):
    assert NotifyHistory(tmp_path / "notify_log.json").entries() == []
    assert incidents == [] and list(tmp_path.iterdir()) == []


def test_a_notification_history_without_a_file_lives_in_memory(tmp_path, monkeypatch, incidents):
    monkeypatch.chdir(tmp_path)
    history = NotifyHistory(keep=2)
    for n in range(3):
        history.add("qq", "group", f"event {n}", True)
    assert [e["summary"] for e in history.entries()] == ["event 2", "event 1"]
    assert list(tmp_path.iterdir()) == []


# ---- start-up ----

def test_the_app_starts_on_a_damaged_live_stream_state_and_history_and_alerts_the_owner(tmp_path, monkeypatch):
    monkeypatch.setitem(Notifier.registry, "rec", AlertRecorder)
    monkeypatch.setenv("KIRI_T_REC", "https://alerts.test/hook")
    (tmp_path / "notify_state.json").write_text("{damaged", encoding="utf-8")
    (tmp_path / "notify_log.json").write_text("[damaged", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": []},  # nobody to poll: no network
        "notify": {"state_file": str(tmp_path / "notify_state.json"), "log_file": str(tmp_path / "notify_log.json"),
                   "rec": {"webhook_env": "KIRI_T_REC"}},
    }), encoding="utf-8")
    previous = set_incident_hook(None)
    try:
        app = create_app(cfg)
        rec = app.state.notifier.notifiers[0]
        with TestClient(app) as client:
            assert wait_for(lambda: len(rec.alerts) == 2)
            assert client.get("/healthz").json() == {"ok": True}
            assert {e["summary"] for e in client.get("/api/notify").json()["log"]} == set(rec.alerts)
    finally:
        set_incident_hook(previous)
    (state_backup,) = backups(tmp_path, "notify_state.json")
    (log_backup,) = backups(tmp_path, "notify_log.json")
    assert sorted(rec.alerts) == sorted([
        f"notify_state.json 损坏，已备份为 {state_backup.name}，已按空文件继续运行",
        f"notify_log.json 损坏，已备份为 {log_backup.name}，已按空文件继续运行",
    ])


def wait_for(condition, timeout: float = 5) -> bool:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True
