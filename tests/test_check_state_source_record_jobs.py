"""The deploy check and the source record sheet: the check reads the jobs file and the record
of rows written like every other state file, reports one it cannot read, and rows waiting to
be written never hold a deploy (they are written after the restart). Made-up ids and links."""

from __future__ import annotations

import pytest

from backend import check_state
from backend.notify.qq import SourceRecordSheet

from .source_record_fakes import FILE_ID, TAB_ID, make
from .test_check_state import files, qq_on, write_config  # noqa: F401  (qq_on is a fixture)
from .test_source_record_processed import processed, store

NAMES = ("source_record_jobs.json", "source_record_rows.json")


def test_rows_waiting_to_be_written_do_not_hold_a_deploy_and_are_left_alone(tmp_path, qq_on, capsys):  # noqa: F811
    cfg = write_config(tmp_path)
    data = tmp_path / "data"
    s = make(groups=store(data, ("G1", {"streamers": ["A"], "source_record_sheet": SourceRecordSheet(FILE_ID, TAB_ID)})))
    processed(s.keeper, stream_id="v1")
    s.settle()  # one row written: the record is not empty
    processed(s.keeper, stream_id="v2")  # and one waits
    before = files(data)
    assert set(NAMES) <= set(before)

    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 0
    assert "状态文件正常" in capsys.readouterr().out
    assert files(data) == before


@pytest.mark.parametrize("damage", ["{oops", '[{"file_id": "F"}]', '{"not": "a list"}'])
def test_a_source_record_state_file_the_new_code_cannot_read_is_reported_and_left_alone(tmp_path, qq_on, capsys,  # noqa: F811
                                                                                       damage):
    cfg = write_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    for name in NAMES:
        (data / name).write_text(damage, encoding="utf-8")

    assert check_state.main(["--config", str(cfg)]) == 1

    out = capsys.readouterr().out
    for name in NAMES:
        (line,) = [x for x in out.splitlines() if name in x]
        assert "备份" in line and "清空" in line
    assert files(data) == dict.fromkeys(NAMES, damage)  # no backups
