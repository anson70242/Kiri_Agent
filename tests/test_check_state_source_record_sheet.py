"""The QQ groups file with and without a group's source record sheet: the file as it is on the
server today (no such key) and the new shape both load unchanged and pass the deploy check; a
sheet saved as anything but {file_id, tab_id} is damaged operator data. Made-up ids."""

from __future__ import annotations

import json

import pytest

from backend import check_state
from backend.monitor import Streamer
from backend.notify.qq import QQGroups, SourceRecordSheet
from backend.state_file import StateFileError

from .test_check_state import files, qq_on, write_config  # noqa: F401  (qq_on is a fixture)

TODAY = {"openid": "G1", "name": "向日葵字幕组", "enabled": True, "streamers": ["A"], "kinds": ["live"],
         "group_files_to_drive": False, "group_file_notices": False,
         "status": "active", "added_at": "2026-11-01T00:00:00+00:00"}
WITH_SHEET = {**TODAY, "openid": "G2", "source_record_sheet": {"file_id": "DFakeFile123", "tab_id": "TAB0x1"}}


def with_groups(tmp_path, *groups):
    cfg = write_config(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "qq_groups.json").write_text(json.dumps(list(groups), ensure_ascii=False), encoding="utf-8")
    return cfg


def test_both_shapes_pass_the_deploy_check_and_are_left_alone(tmp_path, qq_on, capsys):  # noqa: F811
    cfg = with_groups(tmp_path, TODAY, WITH_SHEET)
    before = files(tmp_path / "data")

    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 0
    assert "状态文件正常" in capsys.readouterr().out
    assert files(tmp_path / "data") == before


def test_both_shapes_load_as_they_are(tmp_path):
    path = tmp_path / "qq_groups.json"
    path.write_text(json.dumps([TODAY, WITH_SHEET], ensure_ascii=False), encoding="utf-8")
    store = QQGroups(path)
    assert store.get("G1") == TODAY
    assert store.get("G2") == WITH_SHEET
    assert [g.sheet for g in store.recording(Streamer("A", "youtube", "@a"))] == [SourceRecordSheet("DFakeFile123", "TAB0x1")]


@pytest.mark.parametrize("value", [None, "https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1", {"file_id": "DFakeFile123"},
                                   {"file_id": "DFakeFile123", "tab_id": ""}, {"file_id": 1, "tab_id": "TAB0x1"}])
def test_a_sheet_saved_as_anything_else_is_damaged_operator_data(tmp_path, qq_on, capsys, value):  # noqa: F811
    cfg = with_groups(tmp_path, {**TODAY, "source_record_sheet": value})
    before = files(tmp_path / "data")

    assert check_state.main(["--config", str(cfg)]) == 1
    (line,) = [x for x in capsys.readouterr().out.splitlines() if "qq_groups.json" in x]
    assert "拒绝启动" in line
    assert files(tmp_path / "data") == before
    with pytest.raises(StateFileError):
        QQGroups(tmp_path / "data" / "qq_groups.json")
