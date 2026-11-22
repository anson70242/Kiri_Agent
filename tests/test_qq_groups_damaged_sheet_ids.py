"""A damaged QQ groups file is reported (to the log and the owner's alert) without the source
record sheet's file id and tab id, which are the sheet's address. Made-up ids."""

from __future__ import annotations

import json

import pytest

from backend.notify.qq import QQGroups
from backend.state_file import StateFileError

FILE_ID, TAB_ID = "SECRETFILEID9", "tabSECRET9"


def damaged(tmp_path, group: dict) -> StateFileError:
    path = tmp_path / "qq_groups.json"
    path.write_text(json.dumps([group]), encoding="utf-8")
    with pytest.raises(StateFileError) as e:
        QQGroups(path)
    return e.value


@pytest.mark.parametrize("group", [
    {"openid": "G1", "group_files_to_drive": "yes", "source_record_sheet": {"file_id": FILE_ID, "tab_id": TAB_ID}},
    {"openid": "G1", "source_record_sheet": {"file_id": FILE_ID, "tab_id": TAB_ID, "extra": 1}},
    {"openid": 7, "source_record_sheet": {"file_id": FILE_ID, "tab_id": TAB_ID}},
])
def test_the_reason_names_the_group_but_not_its_sheets_ids(tmp_path, group):
    error = damaged(tmp_path, group)
    text = f"{error} {error.reason}"
    assert "not a group" in text
    assert FILE_ID not in text and TAB_ID not in text
