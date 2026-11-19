"""The group-files module retries with the automatic download's interval and maximum of
errors (`download.auto.retry_minutes`, `max_errors`): no settings of its own."""

from __future__ import annotations

import json

from backend.monitor.server import create_app

from .test_group_file_wiring import write_config
from .test_qq import SECRET


def test_the_module_takes_the_automatic_downloads_retry_interval_and_maximum_of_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    config = write_config(tmp_path)
    cfg = json.loads(config.read_text(encoding="utf-8"))
    cfg["download"]["auto"].update(retry_minutes=7, max_errors=5)
    config.write_text(json.dumps(cfg), encoding="utf-8")

    keeper = create_app(config).state.group_files
    assert (keeper.retry_interval, keeper.max_errors) == (7 * 60, 5)
