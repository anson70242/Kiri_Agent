"""The source record sheet retries with the automatic download's interval and maximum of errors
(`download.auto.retry_minutes`, `max_errors`), no settings of its own, and tells the owner
through the operator alert. Through create_app; made-up ids and tokens."""

from __future__ import annotations

import json

from backend.monitor.server import create_app

from .test_source_record_wiring import app, write_config


def test_the_module_takes_the_pipelines_retry_interval_and_maximum_of_errors(tmp_path, monkeypatch):
    app(tmp_path, monkeypatch)  # the environment: QQ and the token
    config = write_config(tmp_path)
    cfg = json.loads(config.read_text(encoding="utf-8"))
    cfg["download"]["auto"].update(retry_minutes=7, max_errors=5)
    config.write_text(json.dumps(cfg), encoding="utf-8")

    keeper = create_app(config).state.source_record
    assert (keeper.retry_interval, keeper.max_errors) == (7 * 60, 5)


def test_by_default_three_tries_fifteen_minutes_apart_told_through_the_operator_alert(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch)
    keeper = a.state.source_record
    assert (keeper.retry_interval, keeper.max_errors) == (15 * 60, 3)
    assert keeper.alert == a.state.notifier.alert
