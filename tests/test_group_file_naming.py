"""The name on the drive: decided from the subfolder's listing just before the upload, with
the record of uploads answering "the same content?". Through the group-files module with a
fake host, a fake drive and a fake clock."""

from __future__ import annotations

from backend.group_files.jobs import GroupFileJobs

from .group_file_fakes import FOLDER, make

SUB = f"{FOLDER}/待校"


class Told:
    """A report that notes the outcomes the worker tells."""

    def __init__(self) -> None:
        self.outcomes: list[tuple[str, str]] = []

    async def uploaded(self, job, place):
        self.outcomes.append(("uploaded", place))

    async def already_there(self, job, place):
        self.outcomes.append(("already there", place))

    async def failed(self, job, reason, given_up):
        self.outcomes.append(("failed", reason))


def test_the_same_file_posted_again_is_not_uploaded_again_and_ends_as_already_there(tmp_path):
    told = Told()
    s = make(tmp_path, report=told)
    s.post("【待校】第3话.ass", b"the file", msg_id="M1")
    s.settle()
    s.post("【待校】第3话.ass", b"the file", msg_id="M2", posted="2026-11-19T06:00:00+08:00")
    s.settle()

    assert [put[1] for put in s.drive.put] == ["【待校】第3话.ass"]
    assert told.outcomes == [("uploaded", f"{SUB}/【待校】第3话.ass"), ("already there", f"{SUB}/【待校】第3话.ass")]
    assert GroupFileJobs(s.jobs_file).load() == []
    assert list(s.temp.rglob("*")) == []


def test_another_file_under_a_name_already_there_is_kept_beside_it_with_the_time_it_was_posted(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", b"first version", msg_id="M1")
    s.settle()
    s.post("【待校】第3话.ass", b"second version", msg_id="M2", posted="2026-11-19T05:41:37+08:00")
    s.settle()

    assert s.drive.folders[SUB] == {"【待校】第3话.ass": b"first version",
                                    "【待校】第3话.2026-11-19-0541.ass": b"second version"}


def test_a_name_already_there_that_the_record_does_not_know_gets_the_time_in_utc_plus_8(tmp_path):
    s = make(tmp_path)
    s.drive.folders[SUB] = {"【待校】第3话.ass": b"put there by hand"}
    s.post("【待校】第3话.ass", b"the file", posted="2026-11-18T22:05:09Z")
    s.settle()

    assert s.drive.put == [(SUB, "【待校】第3话.2026-11-19-0605.ass", b"the file")]
    assert s.drive.folders[SUB]["【待校】第3话.ass"] == b"put there by hand"


def test_when_the_name_with_the_time_is_taken_too_the_seconds_are_added(tmp_path):
    s = make(tmp_path)
    s.drive.folders[SUB] = {"【待校】第3话.ass": b"a", "【待校】第3话.2026-11-19-0541.ass": b"b"}
    s.post("【待校】第3话.ass", b"c", posted="2026-11-19T05:41:37+08:00")
    s.settle()

    assert s.drive.put == [(SUB, "【待校】第3话.2026-11-19-054137.ass", b"c")]


def test_when_the_name_with_the_seconds_is_taken_too_a_number_is_added(tmp_path):
    s = make(tmp_path)
    s.drive.folders[SUB] = {"【待校】第3话.ass": b"a", "【待校】第3话.2026-11-19-0541.ass": b"b",
                            "【待校】第3话.2026-11-19-054137.ass": b"c", "【待校】第3话.2026-11-19-054137-2.ass": b"d"}
    s.post("【待校】第3话.ass", b"e", posted="2026-11-19T05:41:37+08:00")
    s.settle()

    assert s.drive.put == [(SUB, "【待校】第3话.2026-11-19-054137-3.ass", b"e")]


def test_a_new_version_posted_again_is_already_there_under_its_name_with_the_time(tmp_path):
    told = Told()
    s = make(tmp_path, report=told)
    s.post("【待校】第3话.ass", b"first version", msg_id="M1")
    s.settle()
    s.post("【待校】第3话.ass", b"second version", msg_id="M2", posted="2026-11-19T05:41:37+08:00")
    s.settle()
    s.post("【待校】第3话.ass", b"second version", msg_id="M3", posted="2026-11-19T07:00:00+08:00")
    s.settle()

    assert len(s.drive.put) == 2
    assert told.outcomes[-1] == ("already there", f"{SUB}/【待校】第3话.2026-11-19-0541.ass")


def test_a_file_deleted_on_the_drive_is_uploaded_again_under_its_name_when_posted_again(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", b"the file", msg_id="M1")
    s.settle()
    del s.drive.folders[SUB]["【待校】第3话.ass"]  # the owner deleted it by hand
    s.post("【待校】第3话.ass", b"the file", msg_id="M2")
    s.settle()

    assert [put[1] for put in s.drive.put] == ["【待校】第3话.ass", "【待校】第3话.ass"]


def test_the_same_name_in_another_subfolder_is_another_place(tmp_path):
    s = make(tmp_path)
    s.drive.folders[f"{FOLDER}/已校"] = {"第3话.ass": b"other"}
    s.post("【待校】第3话.ass", b"the file", msg_id="M1")
    s.settle()
    s.drive.folders[f"{FOLDER}/已校"]["【待校】第3话.ass"] = b"the file"  # by hand, not in the record
    s.post("【已校】第3话.ass", b"the file", msg_id="M2")
    s.settle()

    assert s.drive.put == [(SUB, "【待校】第3话.ass", b"the file"), (f"{FOLDER}/已校", "【已校】第3话.ass", b"the file")]


def test_the_same_name_in_another_group_is_another_place(tmp_path):
    s = make(tmp_path)
    s.groups.register("GROUP_OPENID_2")
    s.groups.update("GROUP_OPENID_2", name="鲷鱼烧字幕组", group_files_to_drive=True)
    s.post("【待校】第3话.ass", b"the file", msg_id="M1")
    s.settle()
    s.post("【待校】第3话.ass", b"the file", group="GROUP_OPENID_2", msg_id="M2")
    s.settle()

    assert s.drive.put == [(SUB, "【待校】第3话.ass", b"the file"), ("鲷鱼烧字幕组/待校", "【待校】第3话.ass", b"the file")]
