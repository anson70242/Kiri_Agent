"""The deploy check and group files: the check reads the jobs file and the record of uploads
like every other state file, and a deploy waits only for a group file whose upload is
running (one waiting or downloading is picked up again after the restart)."""

from __future__ import annotations

import json
from dataclasses import asdict

from backend import check_state
from backend.group_files import GroupFileJob

from .test_check_state import files, qq_on, write_config  # noqa: F401  (qq_on is a fixture)


def group_file(name, *, downloaded=False, uploading=False, folder="字幕组", subfolder="待校"):
    return GroupFileJob(group="GROUP_OPENID_1", group_folder=folder, subfolder=subfolder, name=name,
                        size=1234, url="https://example.qq.com/f/x", msg_id="MSG_1", posted_at=100.0,
                        downloaded=downloaded, uploading=uploading)


def with_group_files(tmp_path, *jobs):
    """The check-state tests' config, with these jobs in the group-file jobs file."""
    cfg = write_config(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "group_file_jobs.json").write_text(json.dumps([asdict(j) for j in jobs]), encoding="utf-8")
    return cfg


def test_group_files_that_wait_or_download_do_not_hold_a_deploy(tmp_path, qq_on, capsys):  # noqa: F811
    cfg = with_group_files(tmp_path, group_file("【待校】第1话.ass"), group_file("【待校】第2话.ass", downloaded=True))

    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 0

    out = capsys.readouterr().out
    assert "群文件：2 个任务，0 个正在上传" in out
    assert "下载队列：0 个任务" in out and "网盘上传：0 个任务，0 个正在上传" in out  # the other counts as before


def test_a_group_file_whose_upload_is_running_holds_a_deploy_and_is_named(tmp_path, qq_on, capsys):  # noqa: F811
    cfg = with_group_files(tmp_path, group_file("【待校】第1话.ass"),
                           group_file("【熟肉】第2话.mp4", downloaded=True, uploading=True, subfolder="熟肉"))

    assert check_state.main(["--config", str(cfg)]) == 0  # without --require-idle a busy queue is only counted
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 2

    out = capsys.readouterr().out
    assert "群文件：2 个任务，1 个正在上传" in out
    (line,) = [x for x in out.splitlines() if "等它传完再部署" in x]
    assert "字幕组" in line and "【熟肉】第2话.mp4" in line
    assert "第1话" not in out  # a waiting file is not named


def test_damaged_group_file_state_is_reported_as_set_aside_and_left_alone(tmp_path, qq_on, capsys):  # noqa: F811
    cfg = write_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    for name in ("group_file_jobs.json", "group_file_uploads.json"):
        (data / name).write_text("{oops", encoding="utf-8")

    assert check_state.main(["--config", str(cfg)]) == 1

    out = capsys.readouterr().out
    for name in ("group_file_jobs.json", "group_file_uploads.json"):
        (line,) = [x for x in out.splitlines() if name in x]
        assert "备份" in line and "清空" in line
    assert files(data) == {"group_file_jobs.json": "{oops", "group_file_uploads.json": "{oops"}  # no backups
