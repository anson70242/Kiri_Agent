"""The service gives the group-files module's report what it tells with: the QQ bot's API and
groups, the owner's operator alert, the notification history and the module's clock."""

from __future__ import annotations

from .test_group_file_wiring import app


def test_the_report_tells_through_the_qq_bot_and_alerts_the_owner(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch)
    keeper, notifier = a.state.group_files, a.state.notifier
    report = keeper.report

    assert report.api is notifier.qq.api and report.groups is notifier.qq.groups
    assert report.alert == notifier.alert
    assert report.history is notifier.history and notifier.qq.history is notifier.history
    assert report.clock is keeper.clock
