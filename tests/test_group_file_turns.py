"""The group-files worker and drive copies at the drive, through the real Quark module and the
fake CLI: every file downloaded by the time an upload ends goes before a waiting drive copy;
the worker runs nothing at the drive while a drive copy has it; a file waiting for its turn
is a waiting row, not an upload, and is cancelled when its group's switch goes off
meanwhile."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from backend import quark

from .group_file_fakes import GROUP, make
from .helpers import wait_for
from .test_quark_turns import ZIP, Uploads, cli, drive_copy, root  # noqa: F401  (pytest fixtures)

VIDEO = "【熟肉】第1话.mp4"
SUBTITLE = "【待校】第2话.ass"


class Loop:
    """An event loop in a thread of its own, as the service runs the group-files worker
    while the pipeline's drive worker calls the drive from another thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def start(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro):
        return self.start(coro).result(10)


@pytest.fixture
def loop():
    running = Loop()
    yield running
    running.loop.call_soon_threadsafe(running.loop.stop)


@pytest.fixture
def setup(tmp_path, root, cli):  # noqa: F811
    uploads = Uploads(cli)
    d = quark.Quark(root / "quark-drive.cjs", root)
    yield uploads, d, make(tmp_path, drive=d)
    uploads.let_all_go()


def test_a_file_downloaded_while_another_uploads_goes_before_a_waiting_drive_copy(setup, root, loop):  # noqa: F811
    uploads, d, s = setup
    s.post(VIDEO, b"video")
    loop.run(s.keeper.download_next())
    uploading = loop.start(s.keeper.upload_next())
    assert wait_for(lambda: uploads.started == [VIDEO])
    copy = drive_copy(d, root)
    time.sleep(0.3)  # the drive copy asks for its turn
    s.post(SUBTITLE, b"[Script Info]\n")
    loop.run(s.keeper.download_next())

    uploads.let_go(VIDEO)
    assert wait_for(lambda: len(uploads.started) == 2)
    uploads.let_go(uploads.started[1])
    uploads.let_go(ZIP)
    uploading.result(10)
    assert copy.ended() and copy.raised is None
    assert uploads.started == [VIDEO, SUBTITLE, ZIP]


def test_the_worker_runs_nothing_at_the_drive_while_a_drive_copy_uploads(setup, root, cli, loop):  # noqa: F811
    uploads, d, s = setup
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])
    before = len(cli.commands())
    s.post(SUBTITLE, b"[Script Info]\n")
    loop.run(s.keeper.download_next())  # the download does not wait
    uploading = loop.start(s.keeper.upload_next())
    time.sleep(0.3)
    assert len(cli.commands()) == before  # no folder made, nothing listed

    uploads.let_go(ZIP)
    uploads.let_go(SUBTITLE)
    uploading.result(10)
    assert copy.ended() and copy.raised is None
    assert uploads.started == [ZIP, SUBTITLE]


def test_a_file_waiting_for_its_turn_is_a_waiting_row_and_not_marked_uploading(setup, root, loop):  # noqa: F811
    uploads, d, s = setup
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])
    s.post(SUBTITLE, b"[Script Info]\n")
    loop.run(s.keeper.download_next())
    uploading = loop.start(s.keeper.upload_next())
    time.sleep(0.3)

    rows = s.keeper.snapshot()
    assert rows["active"] == []
    assert [(r["file"], r["waiting_for"]) for r in rows["queue"]] == [(SUBTITLE, "upload")]
    assert [j.uploading for j in s.keeper.jobs.load()] == [False]

    uploads.let_go(ZIP)
    assert wait_for(lambda: SUBTITLE in uploads.started)
    rows = s.keeper.snapshot()
    assert [(r["file"], r["doing"]) for r in rows["active"]] == [(SUBTITLE, "upload")] and rows["queue"] == []
    assert [j.uploading for j in s.keeper.jobs.load()] == [True]
    uploads.let_go(SUBTITLE)
    uploading.result(10)
    assert copy.ended()


def test_a_file_whose_switch_goes_off_while_it_waits_for_its_turn_is_cancelled(setup, root, cli, loop):  # noqa: F811
    uploads, d, s = setup
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])
    s.post(SUBTITLE, b"[Script Info]\n")
    loop.run(s.keeper.download_next())
    uploading = loop.start(s.keeper.upload_next())
    time.sleep(0.3)

    s.groups.update(GROUP, group_files_to_drive=False)
    uploads.let_go(ZIP)
    uploading.result(10)
    assert copy.ended()

    assert uploads.started == [ZIP]
    assert not any(c[0] == "create-folder" and "待校" in c[2] for c in cli.commands())
    assert s.keeper.jobs.load() == []
    assert not s.temp.exists() or list(s.temp.iterdir()) == []
