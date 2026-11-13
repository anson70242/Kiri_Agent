"""State files: load with a default, save atomically, one damaged-file policy per file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest

from backend.state_file import Incident, StateFile, StateFileError, set_incident_hook


def test_missing_file_yields_the_default(tmp_path):
    f = StateFile(tmp_path / "queue.json", default=list, policy="recover")
    assert f.load() == []


@dataclass
class Item:
    name: str
    n: int


def items(path, **kw) -> StateFile[list[Item]]:
    """A store of records, like the download queue: JSON list <-> list of Item."""
    return StateFile(path, default=list, decode=lambda raw: [Item(**d) for d in raw],
                     encode=lambda xs: [{"name": x.name, "n": x.n} for x in xs], **kw)


def test_a_saved_value_reads_back_equal_and_the_folder_is_created(tmp_path):
    path = tmp_path / "data" / "sub" / "queue.json"
    items(path, policy="recover").save([Item("あ", 1), Item("b", 2)])

    assert items(path, policy="recover").load() == [Item("あ", 1), Item("b", 2)]
    assert json.loads(path.read_text(encoding="utf-8")) == [{"name": "あ", "n": 1}, {"name": "b", "n": 2}]


def test_a_save_goes_through_its_own_temporary_file_and_leaves_none_behind(tmp_path):
    path = tmp_path / "queue.json"
    (tmp_path / "queue.json.tmp").write_text("left by a save that was interrupted", encoding="utf-8")
    (tmp_path / "queue.tmp").write_text("another file's", encoding="utf-8")

    items(path, policy="recover").save([Item("a", 1)])

    assert sorted(p.name for p in tmp_path.iterdir()) == ["queue.json", "queue.tmp"]
    assert (tmp_path / "queue.tmp").read_text(encoding="utf-8") == "another file's"


def test_a_save_that_fails_leaves_the_file_as_it_was(tmp_path):
    path = tmp_path / "queue.json"
    items(path, policy="recover").save([Item("a", 1)])

    def broken(_):
        raise TypeError("not serializable")

    with pytest.raises(TypeError):
        StateFile(path, default=list, policy="recover", encode=broken).save([Item("b", 2)])
    assert items(path, policy="recover").load() == [Item("a", 1)]


# ---- recover policy (runtime state) ----

BACKUP = re.compile(r"queue\.json\.corrupt-\d{8}-\d{6}")

DAMAGED = {
    "not JSON": b"[{\"name\": \"a\", ",
    "not UTF-8": b"\xff\xfe\x00garbage",
    "the wrong shape": b"{\"name\": \"a\", \"n\": 1}",  # valid JSON, but not a list of records
    "records it can't decode": b"[{\"name\": \"a\"}]",  # a field missing
}


@pytest.mark.parametrize("content", DAMAGED.values(), ids=DAMAGED.keys())
def test_recover_backs_up_a_damaged_file_reports_it_and_returns_the_default(tmp_path, content):
    path = tmp_path / "queue.json"
    path.write_bytes(content)
    incidents: list[Incident] = []
    set_incident_hook(incidents.append)

    assert items(path, policy="recover").load() == []

    (backup,) = [p for p in tmp_path.iterdir() if p.name != "queue.json"]
    assert BACKUP.fullmatch(backup.name) and backup.read_bytes() == content
    assert not path.exists()
    (incident,) = incidents
    assert (incident.path, incident.backup) == (path, backup) and incident.reason
    assert incident.message == f"queue.json 损坏，已备份为 {backup.name}，已按空文件继续运行"


def test_after_recovering_the_store_starts_over_from_empty(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text("{oops", encoding="utf-8")
    incidents: list[Incident] = []
    set_incident_hook(incidents.append)
    store = items(path, policy="recover")

    assert store.load() == []
    assert store.load() == []  # the damaged file is out of the way: no second incident
    store.save([Item("a", 1)])
    assert store.load() == [Item("a", 1)] and len(incidents) == 1


def test_every_incident_is_logged(tmp_path, caplog):
    path = tmp_path / "queue.json"
    path.write_text("{oops", encoding="utf-8")
    set_incident_hook(lambda _: None)
    items(path, policy="recover").load()
    assert "queue.json" in caplog.text and "corrupt-" in caplog.text


def test_with_no_hook_installed_an_incident_is_only_logged(tmp_path, caplog):
    path = tmp_path / "queue.json"
    path.write_text("{oops", encoding="utf-8")
    set_incident_hook(None)
    assert items(path, policy="recover").load() == []
    assert "queue.json" in caplog.text


# ---- strict policy (operator data) ----

@pytest.mark.parametrize("content", DAMAGED.values(), ids=DAMAGED.keys())
def test_strict_refuses_a_damaged_file_and_names_it_and_why(tmp_path, content):
    path = tmp_path / "queue.json"
    path.write_bytes(content)
    incidents: list[Incident] = []
    set_incident_hook(incidents.append)

    with pytest.raises(StateFileError) as e:
        items(path, policy="strict").load()

    assert e.value.path == path and e.value.reason
    assert str(path) in str(e.value) and e.value.reason in str(e.value)
    assert [p.name for p in tmp_path.iterdir()] == ["queue.json"] and path.read_bytes() == content
    assert incidents == []


def test_strict_missing_file_is_still_the_default(tmp_path):
    assert items(tmp_path / "queue.json", policy="strict").load() == []


# ---- reload on change (a file two processes share) ----

def test_changed_tells_when_someone_else_rewrote_the_file(tmp_path):
    path = tmp_path / "users.json"
    mine, theirs = items(path, policy="strict"), items(path, policy="strict")

    assert mine.load() == [] and not mine.changed()
    theirs.save([Item("a", 1)])
    assert mine.changed()
    assert mine.load() == [Item("a", 1)] and not mine.changed()
    mine.save([Item("a", 1), Item("bb", 22)])
    assert not mine.changed() and theirs.changed()


def test_reload_keeps_the_last_good_value_of_a_strict_file_damaged_meanwhile(tmp_path, caplog):
    path = tmp_path / "users.json"
    mine, theirs = items(path, policy="strict"), items(path, policy="strict")
    incidents: list[Incident] = []
    set_incident_hook(incidents.append)
    good = mine.load()
    path.write_text("[{", encoding="utf-8")  # damaged by someone else

    assert mine.reload(good) is good and mine.damaged
    assert mine.reload(good) is good  # not tried again while unchanged
    assert [(i.path, i.backup) for i in incidents] == [(path, None)]  # reported once, no backup
    assert incidents[0].message == "users.json 损坏，继续使用损坏前读到的内容，文件未改动，请修复或从备份恢复"
    assert "users.json" in caplog.text and path.read_text(encoding="utf-8") == "[{"  # left alone
    with pytest.raises(StateFileError):
        mine.load()  # starting on it is still refused
    theirs.save([Item("a", 1)])  # repaired
    assert mine.reload(good) == [Item("a", 1)] and mine.damaged is None
