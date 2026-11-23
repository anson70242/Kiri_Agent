"""The busy mark of the flash stage: only while a video is being cut for its flash transfer
is the flash job marked in the queue file, and only then does the deploy check hold a
deploy; a flash transfer that is uploading in the flash service holds nothing. A video over
the size one file may have is too big to make in a test: the pipeline is told so, and the
cutting is watched from inside the preparing of the files."""

from __future__ import annotations

import shutil

from backend import check_state
from backend.downloader import auto as auto_mod

from .flash_stage_fakes import FakeFlash, processed_live, uploading
from .test_check_state import write_config
from .test_drive_restart import run_until


def video_over_the_limit(monkeypatch, during) -> None:
    """The live's video is over the size one file may have (so preparing cuts it); `during()`
    runs inside the preparing, while the video is being cut."""
    prepare = auto_mod.prepare

    def cutting(*args, **kwargs):
        during()
        return prepare(*args, **kwargs)

    monkeypatch.setattr(auto_mod, "needs_cutting", lambda archive: True)
    monkeypatch.setattr(auto_mod, "prepare", cutting)


def marks(ad) -> list[bool]:
    return [j.cutting for j in ad.load()]


def deploy_check(tmp_path) -> int:
    """The deploy check with --require-idle over the pipeline's queue file as it is now."""
    cfg = write_config(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    shutil.copy(tmp_path / "queue.json", tmp_path / "data" / "download_queue.json")
    return check_state.main(["--config", str(cfg), "--require-idle"])


def test_the_flash_job_is_marked_in_the_queue_file_only_while_its_video_is_cut(tmp_path, monkeypatch):
    flash = FakeFlash(uploading(), uploading(90))
    ad, _, _, _ = processed_live(tmp_path, flash, full=True)
    seen = {}
    video_over_the_limit(monkeypatch, lambda: seen.setdefault("cutting", marks(ad)))
    flash.during_start = lambda files: seen.setdefault("starting", marks(ad))
    run_until(ad, lambda: len(flash.looks) >= 1)

    assert seen == {"cutting": [True], "starting": [True]}  # the try that cuts, up to its start
    assert marks(ad) == [False]  # uploading in the flash service


def test_a_video_that_fits_into_one_file_marks_nothing(tmp_path):
    flash = FakeFlash(uploading())
    ad, _, _, _ = processed_live(tmp_path, flash)
    seen = []
    flash.during_start = lambda files: seen.append(marks(ad))
    run_until(ad, lambda: len(flash.looks) >= 1)

    assert seen == [[False]] and marks(ad) == [False]


def test_the_deploy_check_holds_a_deploy_while_a_video_is_cut_and_not_while_it_uploads(tmp_path, monkeypatch, capsys):
    flash = FakeFlash(uploading())
    ad, _, _, _ = processed_live(tmp_path, flash, full=True)
    codes = []
    video_over_the_limit(monkeypatch, lambda: codes.append(deploy_check(tmp_path)))
    flash.during_start = lambda files: print("-- started --")
    run_until(ad, lambda: len(flash.looks) >= 1)  # cut, started, uploading
    cutting = capsys.readouterr().out.split("-- started --")[0]
    codes.append(deploy_check(tmp_path))
    uploading_out = capsys.readouterr().out

    assert codes == [2, 0]
    assert "闪传：1 个任务，1 个正在切分影片" in cutting
    assert "A title v1" in cutting and "等它切完再部署" in cutting
    assert "闪传：1 个任务，0 个正在切分影片" in uploading_out
