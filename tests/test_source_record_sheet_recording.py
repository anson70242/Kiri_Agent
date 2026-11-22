"""Which QQ groups record a streamer in their source record sheet: the group's sheet is set, the
streamer is ticked for the group (by person, as the notices count streamers) and the bot was not
removed from the group. The group's master switch and its notification types do not count.
Made-up group ids, file ids and tab ids."""

from __future__ import annotations

from backend.monitor import Streamer
from backend.notify.qq import QQGroups, RecordingGroup, SourceRecordSheet

SHEET = SourceRecordSheet("DFakeFile123", "TAB0x1")
A_YOUTUBE, A_TWITCH, A_TWITCASTING = (Streamer("A", "youtube", "@a"), Streamer("A", "twitch", "a_tw"),
                                      Streamer("A", "twitcasting", "a_tc"))
B = Streamer("B", "twitch", "b")


def groups(tmp_path, **settings) -> QQGroups:
    """A store with group G1 set as `settings` say."""
    store = QQGroups(tmp_path / "qq_groups.json")
    store.register("G1")
    store.update("G1", **settings)
    return store


def test_a_group_with_a_sheet_records_the_streamers_it_ticks_on_every_platform(tmp_path):
    store = groups(tmp_path, name="章鱼烧字幕组", streamers=["A"], source_record_sheet=SHEET)
    for s in (A_YOUTUBE, A_TWITCH, A_TWITCASTING):
        assert store.recording(s) == [RecordingGroup("G1", "章鱼烧字幕组", SHEET)], s
    assert store.recording(B) == []


def test_an_older_save_that_ticks_a_channel_records_that_channel(tmp_path):
    store = groups(tmp_path, streamers=["twitch:a_tw"], source_record_sheet=SHEET)
    assert [g.openid for g in store.recording(A_TWITCH)] == ["G1"]
    assert store.recording(A_YOUTUBE) == []


def test_the_master_switch_and_the_notification_types_do_not_count(tmp_path):
    store = groups(tmp_path, enabled=False, kinds=[], streamers=["A"], source_record_sheet=SHEET)
    assert [g.openid for g in store.recording(A_YOUTUBE)] == ["G1"]


def test_a_group_without_a_sheet_records_nothing(tmp_path):
    store = groups(tmp_path, enabled=True, streamers=["A"])
    assert store.recording(A_YOUTUBE) == []
    store.update("G1", source_record_sheet=SHEET)
    store.update("G1", source_record_sheet="")  # cleared
    assert store.recording(A_YOUTUBE) == []


def test_a_group_the_bot_was_removed_from_records_nothing_but_a_muted_one_does(tmp_path):
    store = groups(tmp_path, streamers=["A"], source_record_sheet=SHEET, status="removed")
    assert store.recording(A_YOUTUBE) == []
    store.update("G1", status="muted")  # the group turned the bot's messages off: the sheet is not a message
    assert [g.openid for g in store.recording(A_YOUTUBE)] == ["G1"]


def test_two_groups_on_one_tab_are_both_named(tmp_path):
    store = groups(tmp_path, streamers=["A"], source_record_sheet=SHEET)
    store.register("G2")
    store.update("G2", streamers=["A", "B"], source_record_sheet=SHEET)
    assert [(g.openid, g.sheet) for g in store.recording(A_TWITCH)] == [("G1", SHEET), ("G2", SHEET)]
