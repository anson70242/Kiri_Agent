"""Turns at the drive: the Quark module never runs two uploads at once, drive copies and
group files alike; when an upload ends, a waiting group file goes before a waiting drive
copy, and group files go in the order they came. Run through the fake CLI, with each upload
held until the test lets it go."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from backend import quark
from backend.archive import Archive

from .helpers import sample_archive, wait_for
from .quark_fakes import SHARED, UPLOAD_DELETED, FakeCli, browse, create_folder, progress, uploaded

STEM = "[20261105][柊優花] 歌枠"
ZIP = f"{STEM}.zip"
FOLDER = "某字幕组/待校"


class HeldUpload:
    """One run of the fake CLI's `upload`: half its progress at once, then the same progress
    line again every few hundredths of a second (so the watchdog sees a live upload) until
    the test lets it go, then the rest of a successful upload."""

    def __init__(self, name: str, size: int) -> None:
        self.name, self.size = name, size
        self.go = threading.Event()
        self.killed = threading.Event()

    def __iter__(self):
        yield progress(self.size // 2, self.size)
        while not self.go.wait(0.03):
            if self.killed.is_set():
                return
            yield progress(self.size // 2, self.size)
        yield from uploaded(self.name, self.size)

    def kill(self) -> None:
        self.killed.set()


class Uploads:
    """The fake CLI with every upload held: `started` names the files whose upload began, in
    order; `let_go(name)` ends that upload."""

    def __init__(self, cli: FakeCli) -> None:
        self.started: list[str] = []
        self.held: dict[str, HeldUpload] = {}
        cli.answers.update({"create-folder": [create_folder()], "browse": browse([]), "upload": self._upload,
                            "upload delete": [UPLOAD_DELETED], "share": [SHARED]})

    def _upload(self, argv, cwd):
        path = Path(argv[3])
        held = HeldUpload(path.name, path.stat().st_size)
        self.held[path.name] = held
        self.started.append(path.name)
        return held

    def let_go(self, name: str) -> None:
        assert wait_for(lambda: name in self.held), f"{name} never started"
        self.held[name].go.set()


class Caller(threading.Thread):
    """One caller of the drive in a thread of its own, as the pipeline and the group-files
    module call it: what it got back, or what it raised."""

    def __init__(self, call) -> None:
        super().__init__(daemon=True)
        self.call, self.got, self.raised = call, None, None
        self.start()

    def run(self) -> None:
        try:
            self.got = self.call()
        except Exception as e:  # noqa: BLE001 - the test looks at it
            self.raised = e

    def ended(self) -> bool:
        self.join(5)
        return not self.is_alive()


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCli({})
    monkeypatch.setattr(quark, "run_cli", fake)
    return fake


@pytest.fixture
def uploads(cli):
    return Uploads(cli)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "downloads"


def drive(root, **kw):
    return quark.Quark(Path("/data/quark/scripts/quark-drive.cjs"), root, **kw)


def group_file(tmp_path, name: str) -> Path:
    path = tmp_path / "group-files" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("[Script Info]\n", encoding="utf-8")
    return path


def drive_copy(d, root):
    """The pipeline's call for one archive's drive copy, in a thread."""
    archive = Archive.from_folder(sample_archive(root))
    return Caller(lambda: d(archive, "录播/柊優花", lambda sent, total: None))


def put(d, local):
    """The group-files module's call for one file, in a thread."""
    return Caller(lambda: d.put_file(local, FOLDER))


# ---- one upload at a time ----

def test_a_group_file_waits_while_a_drive_copy_uploads_and_goes_when_it_ends(tmp_path, root, uploads):
    d = drive(root)
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])

    file = put(d, group_file(tmp_path, "第3话.ass"))
    time.sleep(0.3)
    assert uploads.started == [ZIP]  # the group file did not start

    uploads.let_go(ZIP)
    assert wait_for(lambda: uploads.started == [ZIP, "第3话.ass"])
    uploads.let_go("第3话.ass")
    assert copy.ended() and file.ended()
    assert copy.raised is None and copy.got.path == f"录播/柊優花/{ZIP}"
    assert file.raised is None and file.got == f"{FOLDER}/第3话.ass"


def test_a_drive_copy_waits_while_a_group_file_uploads_and_goes_when_it_ends(tmp_path, root, uploads):
    d = drive(root)
    file = put(d, group_file(tmp_path, "第3话.ass"))
    assert wait_for(lambda: uploads.started == ["第3话.ass"])

    copy = drive_copy(d, root)
    time.sleep(0.3)
    assert uploads.started == ["第3话.ass"]

    uploads.let_go("第3话.ass")
    uploads.let_go(ZIP)
    assert file.ended() and copy.ended()
    assert uploads.started == ["第3话.ass", ZIP]
    assert file.raised is None and copy.raised is None


# ---- who goes next ----

def test_when_an_upload_ends_a_waiting_group_file_goes_before_a_drive_copy_that_waited_longer(tmp_path, root,
                                                                                               uploads):
    d = drive(root)
    first = put(d, group_file(tmp_path, "第1话.ass"))
    assert wait_for(lambda: uploads.started == ["第1话.ass"])
    copy = drive_copy(d, root)
    assert wait_for(lambda: d.waiting == [quark.UploadKind.DRIVE_COPY])
    second = put(d, group_file(tmp_path, "第2话.ass"))
    assert wait_for(lambda: d.waiting == [quark.UploadKind.GROUP_FILE, quark.UploadKind.DRIVE_COPY])

    for name in ("第1话.ass", "第2话.ass", ZIP):
        uploads.let_go(name)
    assert first.ended() and second.ended() and copy.ended()
    assert uploads.started == ["第1话.ass", "第2话.ass", ZIP]


def test_waiting_group_files_go_in_the_order_they_came(tmp_path, root, uploads):
    d = drive(root)
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])
    names = [f"第{n}话.ass" for n in range(1, 5)]
    files = []
    for n, name in enumerate(names, 1):
        files.append(put(d, group_file(tmp_path, name)))
        assert wait_for(lambda n=n: len(d.waiting) == n)

    for name in [ZIP, *names]:
        uploads.let_go(name)
    assert copy.ended() and all(f.ended() for f in files)
    assert uploads.started == [ZIP, *names]


def test_a_running_upload_is_not_stopped_by_callers_that_wait(tmp_path, root, uploads):
    d = drive(root)
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])
    files = [put(d, group_file(tmp_path, f"第{n}话.ass")) for n in (1, 2)]
    assert wait_for(lambda: len(d.waiting) == 2)

    uploads.let_go(ZIP)
    assert copy.ended()
    assert copy.raised is None and copy.got.path == f"录播/柊優花/{ZIP}"
    assert not uploads.held[ZIP].killed.is_set()
    for name in ("第1话.ass", "第2话.ass"):
        uploads.let_go(name)
    assert all(f.ended() and f.raised is None for f in files)


def test_time_spent_waiting_for_a_turn_does_not_count_against_the_watchdog(tmp_path, root, uploads):
    d = drive(root, idle_timeout=0.3)
    copy = drive_copy(d, root)  # its progress keeps coming: the watchdog leaves it alone
    assert wait_for(lambda: uploads.started == [ZIP])
    file = put(d, group_file(tmp_path, "第3话.ass"))
    time.sleep(1.0)  # more than three idle times without a line for the waiting file

    uploads.let_go(ZIP)
    uploads.let_go("第3话.ass")
    assert copy.ended() and file.ended()
    assert file.raised is None and file.got == f"{FOLDER}/第3话.ass"


def test_a_failed_upload_gives_the_turn_to_the_next(tmp_path, root, cli, uploads):
    d = drive(root)
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])
    file = put(d, group_file(tmp_path, "第3话.ass"))
    assert wait_for(lambda: len(d.waiting) == 1)

    uploads.held[ZIP].kill()  # the running CLI dies without a result
    assert copy.ended() and copy.raised is not None
    uploads.let_go("第3话.ass")
    assert file.ended() and file.raised is None


# ---- what the drive is busy with ----

def test_the_running_upload_is_seen_with_its_kind_and_progress(tmp_path, root, uploads):
    d = drive(root)
    assert d.uploading is None and d.waiting == []

    copy = drive_copy(d, root)
    assert wait_for(lambda: d.uploading is not None and d.uploading.sent > 0)
    running = d.uploading
    assert running.kind is quark.UploadKind.DRIVE_COPY
    assert running.total == uploads.held[ZIP].size and running.sent == running.total // 2
    assert running.percent in (49, 50)

    file = put(d, group_file(tmp_path, "第3话.ass"))
    assert wait_for(lambda: d.waiting == [quark.UploadKind.GROUP_FILE])
    uploads.let_go(ZIP)
    assert wait_for(lambda: d.uploading is not None and d.uploading.kind is quark.UploadKind.GROUP_FILE)
    uploads.let_go("第3话.ass")
    assert copy.ended() and file.ended()
    assert d.uploading is None and d.waiting == []


def test_a_running_upload_without_a_size_yet_has_no_percentage():
    running = quark.RunningUpload(quark.UploadKind.GROUP_FILE)
    assert running.percent is None
    assert quark.RunningUpload(quark.UploadKind.GROUP_FILE, sent=43, total=100).percent == 43


# ---- outside the turns ----

def test_listing_a_folder_does_not_wait_for_a_running_upload(root, cli, uploads):
    d = drive(root)
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])

    assert d.folder_names(FOLDER) == []  # does not block

    uploads.let_go(ZIP)
    assert copy.ended()


def test_two_quark_objects_do_not_share_turns(tmp_path, root, uploads):
    """The by-hand commands build their own Quark: they are outside the service's turns."""
    service, by_hand = drive(root), drive(root)
    copy = drive_copy(service, root)
    assert wait_for(lambda: uploads.started == [ZIP])

    file = put(by_hand, group_file(tmp_path, "第3话.ass"))
    assert wait_for(lambda: uploads.started == [ZIP, "第3话.ass"])

    uploads.let_go(ZIP)
    uploads.let_go("第3话.ass")
    assert copy.ended() and file.ended()
