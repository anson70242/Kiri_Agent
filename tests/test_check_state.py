"""The deploy check (`uv run -m backend.check_state`): the new code reads every state file
the service would load, says which are damaged and how busy the download queue is, and
changes nothing on disk."""

from __future__ import annotations

import json

import pytest

from backend import check_state
from backend.downloader.auto import Job

from .helpers import live


def write_config(tmp_path):
    data = tmp_path / "data"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a"}],
                    "streamers_file": str(data / "streamers.json")},
        "notify": {"discord": {"enabled": False}, "state_file": str(data / "notify_state.json"),
                   "log_file": str(data / "notify_log.json"),
                   "qq": {"app_id_env": "KIRI_T_QQ_ID", "secret_env": "KIRI_T_QQ_SECRET",
                          "groups_file": str(data / "qq_groups.json")}},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(data / "download_queue.json")},
                     "chat_retry": {"state_file": str(data / "pending_chat.json")}},
        "auth": {"enabled": True, "users_file": str(data / "users.json"),
                 "sessions_file": str(data / "sessions.json")},
    }), encoding="utf-8")
    return cfg


@pytest.fixture
def qq_on(monkeypatch):
    monkeypatch.setenv("KIRI_T_QQ_ID", "1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", "s")


def files(folder) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())} if folder.exists() else {}


def test_a_fresh_install_passes_and_nothing_is_written(tmp_path, qq_on, capsys):
    assert check_state.main(["--config", str(write_config(tmp_path)), "--require-idle"]) == 0
    assert files(tmp_path / "data") == {}  # not even the roster seeded from the config
    assert "状态文件正常" in capsys.readouterr().out


STATE_FILES = ["streamers.json", "users.json", "sessions.json", "qq_groups.json", "download_queue.json",
               "pending_chat.json", "notify_state.json", "notify_log.json"]


def test_every_damaged_state_file_is_named_with_what_the_service_would_do_and_left_alone(tmp_path, qq_on, capsys):
    cfg = write_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    for name in STATE_FILES:
        (data / name).write_text("{oops", encoding="utf-8")

    assert check_state.main(["--config", str(cfg)]) == 1

    out = capsys.readouterr().out
    for name in ("streamers.json", "users.json", "qq_groups.json"):  # operator data
        (line,) = [x for x in out.splitlines() if name in x]
        assert "拒绝启动" in line
    for name in ("sessions.json", "download_queue.json", "pending_chat.json", "notify_state.json", "notify_log.json"):
        (line,) = [x for x in out.splitlines() if name in x]
        assert "备份" in line and "清空" in line
    assert files(data) == dict.fromkeys(sorted(STATE_FILES), "{oops")  # untouched, no backups


def test_a_busy_download_queue_fails_only_when_idle_is_required(tmp_path, capsys):
    cfg = write_config(tmp_path)
    (tmp_path / "data").mkdir()
    job = Job(live(), queued_at=100.0, next_try=200.0)
    (tmp_path / "data" / "download_queue.json").write_text(json.dumps([job.to_dict()]), encoding="utf-8")

    assert check_state.main(["--config", str(cfg)]) == 0
    assert "下载队列：1 个任务" in capsys.readouterr().out
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 2
    assert "等处理完再部署" in capsys.readouterr().out
