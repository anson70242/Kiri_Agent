"""The group is told what became of a filed file, when its notice switch is on: on the drive,
already there, failed and retried later, given up on, or waiting for the drive's
authorization. A quick outcome is a reply to the member's message; later, or when QQ refuses
the reply, an ordinary message. Every failure also reaches the owner as an operator alert,
whatever the switch says; successes do not."""

from __future__ import annotations

import json
from datetime import datetime

import httpx

from backend.drive import LOGIN_COMMAND, DriveAuthorizationLapsed, DriveFailed
from backend.group_files.jobs import GroupFileJobs
from backend.group_files.report import GroupFileReport
from backend.notify.base import EventKind
from backend.notify.history import NotifyHistory
from backend.notify.qq import KIND_LABELS, QQApi, QQSettings

from .group_file_fakes import FOLDER, GROUP, POSTED, SECRET, FakeDrive, FakeQQ, make

INTERVAL = 600.0
MINUTE = 60.0
POSTED_AT = datetime.fromisoformat(POSTED).timestamp()


class QQ(FakeQQ):
    """The QQ API; `refuse_replies`: a message sent as a reply is refused, as QQ does after
    its 5 minutes; `down`: every message fails."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse_replies = False
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            body = json.loads(request.content)
            if self.down:
                return httpx.Response(500, json={"code": 500, "message": "server error"})
            if self.refuse_replies and body.get("msg_id"):
                return httpx.Response(400, json={"code": 40034024, "message": "msg_id expired"})
        return super().handle(request)

    def sent(self) -> list[tuple[str, str, str | None]]:
        """(group, text, the message it replies to) of each message QQ took."""
        return [(g, b["markdown"]["content"], b.get("msg_id")) for g, b in self.messages]


class Told:
    def __init__(self, tmp_path, notices: bool = True, **kw) -> None:
        self.s = make(tmp_path, retry_interval=INTERVAL, max_errors=3, **kw)
        if notices:
            self.s.groups.update(GROUP, group_file_notices=True)
        self.qq = QQ()
        self.alerts: list[str] = []
        self.history = NotifyHistory()
        self.s.keeper.report = GroupFileReport(
            QQApi(QQSettings("APP1", SECRET), self.qq.client()), self.s.groups,
            alert=self.alerts.append, history=self.history, clock=self.s.clock)
        # The member posted the file just now, by the module's clock.
        self.s.clock.t = POSTED_AT

    def post(self, name: str, data: bytes = b"subtitle", **kw) -> str:
        return self.s.post(name, data, **kw)

    def settle(self) -> None:
        self.s.settle()


def test_a_file_on_the_drive_is_told_as_a_reply_to_the_members_message(tmp_path):
    t = Told(tmp_path)
    t.post("【待校】第3话.ass", msg_id="MSG7")
    t.settle()

    assert t.qq.sent() == [(GROUP, "☁️ 群文件已上传网盘\n【待校】第3话.ass\n"
                                   f"网盘位置：{FOLDER}/待校/【待校】第3话.ass\n切片小助手 群文件", "MSG7")]
    assert t.alerts == []


def text(*lines: str) -> str:
    return "\n".join([*lines, "切片小助手 群文件"])


def test_a_notice_later_than_the_reply_window_is_an_ordinary_message(tmp_path):
    t = Told(tmp_path)
    t.post("【待校】第3话.ass", msg_id="MSG7")
    t.s.clock.t += 4.5 * MINUTE  # the margin before QQ's 5 minutes
    t.settle()

    assert [(g, reply) for g, _, reply in t.qq.sent()] == [(GROUP, None)]


def test_a_reply_refused_by_qq_goes_out_once_as_an_ordinary_message(tmp_path):
    t = Told(tmp_path)
    t.qq.refuse_replies = True
    t.post("【待校】第3话.ass", msg_id="MSG7")
    t.settle()

    assert t.qq.sent() == [(GROUP, text("☁️ 群文件已上传网盘", "【待校】第3话.ass",
                                        f"网盘位置：{FOLDER}/待校/【待校】第3话.ass"), None)]
    assert [(e["target"], e["summary"], e["ok"]) for e in t.history.entries()] == [
        (GROUP, "☁️ 群文件已上传网盘：【待校】第3话.ass", True)]


def test_the_same_file_again_is_told_as_already_there(tmp_path):
    t = Told(tmp_path)
    t.post("【熟肉】第3话.mp4", b"video", msg_id="MSG1")
    t.settle()
    t.post("【熟肉】第3话.mp4", b"video", msg_id="MSG2")
    t.settle()

    assert t.qq.sent()[1] == (GROUP, text("☁️ 群文件已在网盘，没有重复上传", "【熟肉】第3话.mp4",
                                          f"网盘位置：{FOLDER}/熟肉/【熟肉】第3话.mp4"), "MSG2")
    assert t.alerts == []


def test_a_renamed_file_is_told_with_its_name_on_the_drive(tmp_path):
    t = Told(tmp_path)
    t.s.drive.folders[f"{FOLDER}/待校"] = {"【待校】第3话.ass": b"older"}
    t.post("【待校】第3话.ass", b"newer")
    t.settle()

    (_, sent, _), = t.qq.sent()
    assert sent.splitlines()[1:3] == ["【待校】第3话.ass", f"网盘位置：{FOLDER}/待校/【待校】第3话.2026-11-19-0541.ass"]


def test_a_failure_is_told_at_the_first_error_and_at_the_give_up_and_alerted_both_times(tmp_path):
    t = Told(tmp_path)
    t.s.drive.error = DriveFailed("网盘空间不足")
    t.post("【待校】第3话.ass", msg_id="MSG7")
    for _ in range(3):  # the maximum of errors
        t.settle()
        t.s.clock.t += INTERVAL

    assert t.qq.sent() == [
        (GROUP, text("❗ 群文件上传网盘失败", "【待校】第3话.ass", "网盘空间不足", "稍后自动重试"), "MSG7"),
        (GROUP, text("❗ 群文件上传网盘失败，已放弃", "【待校】第3话.ass", "网盘空间不足"), None),
    ]
    assert t.alerts == [
        f"QQ 群「{FOLDER}」群文件上传网盘失败\n【待校】第3话.ass\n网盘空间不足\n稍后自动重试",
        f"QQ 群「{FOLDER}」群文件上传网盘失败，已放弃\n【待校】第3话.ass\n网盘空间不足",
    ]


def test_a_success_after_a_failure_is_told_like_any_success(tmp_path):
    t = Told(tmp_path)
    t.s.drive.error = DriveFailed("网盘空间不足")
    t.post("【待校】第3话.ass")
    t.settle()
    t.s.drive.error = None
    t.s.clock.t += INTERVAL
    t.settle()

    assert [sent.splitlines()[0] for _, sent, _ in t.qq.sent()] == ["❗ 群文件上传网盘失败", "☁️ 群文件已上传网盘"]
    assert len(t.alerts) == 1


def test_a_failed_download_is_told_as_well(tmp_path):
    t = Told(tmp_path)
    t.post("【待校】第3话.ass", url="https://grpfile.ftn.qq.com/gone")
    del t.s.host.files["https://grpfile.ftn.qq.com/gone"]
    t.settle()

    (_, sent, _), = t.qq.sent()
    assert sent.startswith("❗ 群文件上传网盘失败\n【待校】第3话.ass\n") and "稍后自动重试" in sent
    assert len(t.alerts) == 1


def test_a_lapsed_authorization_tells_each_group_and_alerts_the_owner_once_per_lapse(tmp_path):
    t = Told(tmp_path)
    t.s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")
    t.post("【待校】第3话.ass", msg_id="MSG1")
    t.post("【待校】第4话.ass", msg_id="MSG2")
    t.settle()
    t.s.clock.t += INTERVAL
    t.settle()  # still lapsed: silent

    assert t.qq.sent() == [
        (GROUP, text("❗ 群文件上传网盘失败", "【待校】第3话.ass", "网盘授权已失效，重新授权后会自动上传"), "MSG1"),
        (GROUP, text("❗ 群文件上传网盘失败", "【待校】第4话.ass", "网盘授权已失效，重新授权后会自动上传"), "MSG2"),
    ]
    (alert,) = t.alerts
    assert alert.startswith(f"QQ 群「{FOLDER}」群文件上传网盘失败\n【待校】第3话.ass\n")
    assert LOGIN_COMMAND in alert

    t.s.drive.error = None  # authorized again: the lapse is over
    t.s.clock.t += INTERVAL
    t.settle()
    t.s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")  # a new lapse
    t.post("【待校】第5话.ass")
    t.settle()
    assert len(t.alerts) == 2


def test_with_the_notice_switch_off_the_group_hears_nothing_and_the_owner_is_alerted(tmp_path):
    t = Told(tmp_path, notices=False)
    t.post("【待校】第3话.ass")
    t.settle()
    t.s.drive.error = DriveFailed("网盘空间不足")
    t.post("【待校】第4话.ass")
    t.settle()

    assert t.qq.sent() == [] and t.history.entries() == []
    assert t.alerts == [f"QQ 群「{FOLDER}」群文件上传网盘失败\n【待校】第4话.ass\n网盘空间不足\n稍后自动重试"]


class Removing(FakeDrive):
    """A drive during whose upload the bot is removed from the group."""

    def __init__(self, groups) -> None:
        super().__init__()
        self.groups = groups

    def put_file(self, local, folder, progress=None):
        self.groups.update(GROUP, status="removed")
        return super().put_file(local, folder, progress)


def test_a_group_marked_as_removed_is_not_told(tmp_path):
    t = Told(tmp_path)
    t.s.keeper.drive = Removing(t.s.groups)
    t.post("【待校】第3话.ass")
    t.settle()

    assert t.qq.sent() == []


def test_a_file_no_rule_keeps_and_a_group_whose_files_stay_get_nothing(tmp_path):
    t = Told(tmp_path)
    t.post("会议记录.ass")
    t.settle()
    t.s.groups.update(GROUP, group_files_to_drive=False)
    t.post("【待校】第3话.ass")
    t.settle()

    assert t.qq.sent() == [] and t.alerts == []


def test_a_name_with_markdown_characters_is_shown_as_typed(tmp_path):
    t = Told(tmp_path)
    t.post("【待校】[第3话]_final_v2.ass")
    t.settle()

    (_, sent, _), = t.qq.sent()
    assert sent.splitlines()[1:3] == ["【待校】［第3话］＿final＿v2.ass",
                                      f"网盘位置：{FOLDER}/待校/【待校】［第3话］＿final＿v2.ass"]


def test_a_message_qq_does_not_take_is_noted_and_the_file_is_on_the_drive_all_the_same(tmp_path):
    t = Told(tmp_path)
    t.qq.down = True
    t.post("【待校】第3话.ass")
    t.settle()

    assert t.s.drive.put == [(f"{FOLDER}/待校", "【待校】第3话.ass", b"subtitle")]
    assert GroupFileJobs(t.s.jobs_file).load() == []
    ((target, summary, ok, error),) = [(e["target"], e["summary"], e["ok"], e["error"]) for e in t.history.entries()]
    assert (target, summary, ok) == (GROUP, "☁️ 群文件已上传网盘：【待校】第3话.ass", False) and error


def test_the_notices_are_no_pipeline_event_and_carry_no_cover(tmp_path):
    t = Told(tmp_path)
    t.post("【待校】第3话.ass")
    t.settle()

    ((_, body),) = t.qq.messages
    assert body["msg_type"] == 2 and "force_verify_image_resource" not in body
    assert not [k for k in KIND_LABELS if "group" in k] and not [k for k in EventKind if "group" in k]


def test_a_failed_download_does_not_end_a_lapse(tmp_path):
    t = Told(tmp_path)
    t.s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")
    t.post("【待校】第3话.ass")
    t.settle()
    t.post("【待校】第4话.ass", url="https://grpfile.ftn.qq.com/gone")  # the file host has lost it
    del t.s.host.files["https://grpfile.ftn.qq.com/gone"]
    t.post("【待校】第5话.ass")
    t.settle()

    assert [a.splitlines()[1] for a in t.alerts] == ["【待校】第3话.ass", "【待校】第4话.ass"]
    assert LOGIN_COMMAND in t.alerts[0]
