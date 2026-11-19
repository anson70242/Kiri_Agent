"""A file whose download is done but whose upload cannot start because another upload has the
drive is told to its group once, after it has waited a minute: that it is queued, what the
drive is busy with (a drive copy, or another group file, with how far along when known), and
that it goes up by itself. A shorter wait is silent, and so is a file waiting for a retry or
for the drive's authorization until its retry comes."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager

import httpx

from backend.drive import DriveAuthorizationLapsed, DriveFailed
from backend.group_files import GroupFileKeeper, worker
from backend.quark import RunningUpload, UploadKind

from .group_file_fakes import FOLDER, GROUP, FakeDrive
from .test_group_file_messages import INTERVAL, MINUTE, Told, text

DRIVE_COPY_UPLOAD = RunningUpload(UploadKind.DRIVE_COPY, sent=43, total=100)


class HeldDrive(FakeDrive):
    """A drive whose turn comes only when the test lets it go (`go`), like Quark's while
    another upload has the drive; `asked`: the worker asked for it."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = threading.Event()
        self.go = threading.Event()

    @asynccontextmanager
    async def turn(self, kind):
        self.asked.set()
        assert await asyncio.to_thread(self.go.wait, 10)
        yield self


class SlowDrive(FakeDrive):
    """A drive whose turn comes at once and whose uploads end only when the test lets them
    go (`go`); `called`: an upload started."""

    def __init__(self) -> None:
        super().__init__()
        self.called = threading.Event()
        self.go = threading.Event()

    def put_file(self, local, folder, progress=None):
        self.called.set()
        assert self.go.wait(10)
        return super().put_file(local, folder, progress)


async def until(check) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)


def test_a_file_held_behind_a_drive_copy_is_told_after_a_minute_then_told_it_is_on_the_drive(tmp_path):
    drive = HeldDrive()
    t = Told(tmp_path, drive=drive)
    t.post("【待校】第3话.ass", msg_id="MSG7")
    keeper = t.s.keeper

    async def go() -> None:
        await keeper.download_next()
        drive.uploading = DRIVE_COPY_UPLOAD
        upload = asyncio.create_task(keeper.upload_next())
        await until(drive.asked.is_set)
        t.s.clock.t += MINUTE
        await keeper.tell_waiting()
        drive.uploading = None
        drive.go.set()
        await upload

    asyncio.run(go())

    assert t.qq.sent() == [
        (GROUP, text("⏳ 群文件排队中", "【待校】第3话.ass", "网盘正在上传一场录播（43%）", "轮到后自动上传"), "MSG7"),
        (GROUP, text("☁️ 群文件已上传网盘", "【待校】第3话.ass", f"网盘位置：{FOLDER}/待校/【待校】第3话.ass"), "MSG7"),
    ]
    assert [(e["target"], e["summary"], e["ok"]) for e in t.history.entries()][-1] == (
        GROUP, "⏳ 群文件排队中：【待校】第3话.ass", True)
    assert t.alerts == []


def downloaded(t: Told, *names: str) -> None:
    """The members post `names`; their downloads are done, no upload has started."""
    for i, name in enumerate(names):
        t.post(name, msg_id=f"MSG{i + 1}")

    async def go() -> None:
        while await t.s.keeper.download_next():
            pass

    asyncio.run(go())


def look(t: Told) -> None:
    asyncio.run(t.s.keeper.tell_waiting())


def test_a_shorter_wait_is_silent_and_a_file_is_told_once(tmp_path):
    t = Told(tmp_path)
    downloaded(t, "【待校】第3话.ass")
    t.s.drive.uploading = DRIVE_COPY_UPLOAD
    t.s.clock.t += MINUTE - 1
    look(t)
    assert t.qq.sent() == []

    t.s.clock.t += 1
    look(t)
    t.s.clock.t += 5 * MINUTE
    look(t)
    assert [sent.splitlines()[0] for _, sent, _ in t.qq.sent()] == ["⏳ 群文件排队中"]


def test_a_restart_does_not_tell_a_file_again(tmp_path):
    t = Told(tmp_path)
    downloaded(t, "【待校】第3话.ass")
    t.s.drive.uploading = DRIVE_COPY_UPLOAD
    t.s.clock.t += MINUTE
    look(t)
    s = t.s
    s.keeper = GroupFileKeeper(s.groups, s.drive, jobs_file=s.jobs_file, temp=s.temp, client=s.host.client(),
                               clock=s.clock, report=s.keeper.report)
    look(t)

    assert len(t.qq.sent()) == 1


def test_a_file_behind_another_group_file_is_told_so_without_a_percentage_not_yet_known(tmp_path):
    drive = SlowDrive()
    t = Told(tmp_path, drive=drive)
    t.post("【熟肉】第3话.mp4", b"video", msg_id="MSG1")
    t.post("【待校】第3话.ass", msg_id="MSG2")
    keeper = t.s.keeper

    async def go() -> None:
        while await keeper.download_next():
            pass
        upload = asyncio.create_task(keeper.upload_next())
        await until(drive.called.is_set)
        drive.uploading = RunningUpload(UploadKind.GROUP_FILE)  # the video's, the CLI has not said how far
        t.s.clock.t += MINUTE
        await keeper.tell_waiting()
        drive.uploading = None
        drive.go.set()
        await upload

    asyncio.run(go())

    assert t.qq.sent()[0] == (GROUP, text("⏳ 群文件排队中", "【待校】第3话.ass", "网盘正在上传其他群文件", "轮到后自动上传"),
                              "MSG2")
    assert [sent.splitlines()[0] for _, sent, _ in t.qq.sent()[1:]] == ["☁️ 群文件已上传网盘"] * 2  # one turn, both


def test_a_file_waiting_for_a_retry_or_the_authorization_is_not_told_it_is_queued_until_its_retry_comes(tmp_path):
    t = Told(tmp_path)
    t.s.drive.error = DriveFailed("网盘空间不足")
    t.post("【待校】第3话.ass", msg_id="MSG1")
    t.settle()
    t.s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")
    t.post("【待校】第4话.ass", msg_id="MSG2")
    t.settle()
    t.s.drive.error = None
    t.s.drive.uploading = DRIVE_COPY_UPLOAD

    t.s.clock.t += 2 * MINUTE  # waiting for their next try
    look(t)
    assert [sent.splitlines()[0] for _, sent, _ in t.qq.sent()] == ["❗ 群文件上传网盘失败"] * 2

    t.s.clock.t += INTERVAL - 2 * MINUTE + MINUTE - 1  # due again, behind the drive copy for less than a minute
    look(t)
    assert len(t.qq.sent()) == 2
    t.s.clock.t += 1
    look(t)
    assert [sent.splitlines()[0] for _, sent, _ in t.qq.sent()[2:]] == ["⏳ 群文件排队中"] * 2


def test_a_file_whose_download_failed_once_is_told_when_it_then_waits_behind_a_drive_copy(tmp_path):
    t = Told(tmp_path)
    url = t.post("【待校】第3话.ass", msg_id="MSG1")
    t.s.host.answers[url] = httpx.Response(500)
    asyncio.run(t.s.keeper.download_next())
    del t.s.host.answers[url]
    t.s.clock.t += INTERVAL
    asyncio.run(t.s.keeper.download_next())  # the retry gets it
    t.s.drive.uploading = DRIVE_COPY_UPLOAD
    t.s.clock.t += MINUTE
    look(t)

    assert [sent.splitlines()[0] for _, sent, _ in t.qq.sent()] == ["❗ 群文件上传网盘失败", "⏳ 群文件排队中"]


def test_with_the_notice_switch_off_the_group_is_not_told_and_the_owner_never_is(tmp_path):
    t = Told(tmp_path, notices=False)
    downloaded(t, "【待校】第3话.ass")
    t.s.drive.uploading = DRIVE_COPY_UPLOAD
    t.s.clock.t += MINUTE
    look(t)

    assert t.qq.sent() == [] and t.history.entries() == [] and t.alerts == []


def test_the_running_worker_tells_a_waiting_file(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WAITING_CHECK", 0.01)
    drive = HeldDrive()
    drive.uploading = DRIVE_COPY_UPLOAD
    t = Told(tmp_path, drive=drive)
    t.post("【待校】第3话.ass")

    async def go() -> None:
        running = asyncio.create_task(t.s.keeper.run())
        await until(drive.asked.is_set)
        t.s.clock.t += MINUTE
        await until(lambda: len(t.qq.sent()) == 1)
        drive.uploading = None
        drive.go.set()
        await until(lambda: len(t.qq.sent()) == 2)
        running.cancel()

    asyncio.run(go())

    assert [sent.splitlines()[0] for _, sent, _ in t.qq.sent()] == ["⏳ 群文件排队中", "☁️ 群文件已上传网盘"]
