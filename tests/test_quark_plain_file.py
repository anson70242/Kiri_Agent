"""A plain local file put into a folder of the drive, and a folder's names read, through the
Quark module; run through the fake CLI with the answers the real CLI gave (ticket 01)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import quark
from backend.drive import DriveAuthorizationLapsed, DriveFailed

from .quark_fakes import (AUTH_URL, DEVICE_ID, FOLDER_ID, LAPSED_103, NOISE, RECORD_ID, UPLOAD_DELETED, FakeCli,
                          FakeProcess, browse, create_folder, lapsed_11001, line, needs_authorization, progress, result,
                          upload_failed, uploaded)

NAME = "【待校】第3话.ass"
FOLDER = "某字幕组/待校"


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCli({})
    monkeypatch.setattr(quark, "run_cli", fake)
    return fake


@pytest.fixture
def root(tmp_path):
    return tmp_path / "downloads"


@pytest.fixture
def local(tmp_path):
    """The local file to put on the drive, in a place of the caller's (not the downloads root)."""
    path = tmp_path / "group-files" / NAME
    path.parent.mkdir()
    path.write_text("[Script Info]\n", encoding="utf-8")
    return path


def drive(root, **kw):
    return quark.Quark(Path("/data/quark/scripts/quark-drive.cjs"), root, **kw)


def putting(cli, files=(), answer=None):
    """The fake CLI: the folder made, listed with `files`, and the upload answered with
    `answer` (a successful upload of what it is given by default). Returns what the upload
    saw: the file's name and contents while it was being uploaded."""
    seen = {}

    def upload(argv, cwd):
        path = Path(argv[3])
        seen.update(name=path.name, text=path.read_text(encoding="utf-8"))
        return answer if answer is not None else uploaded(path.name, path.stat().st_size)

    cli.answers.update({"create-folder": NOISE + [create_folder()], "browse": browse(list(files)), "upload": upload})
    return seen


# ---- putting a file ----

def test_a_local_file_is_uploaded_under_its_name_into_the_folder_made_from_the_top_of_the_drive(root, cli, local):
    seen = putting(cli)

    path = drive(root).put_file(local, FOLDER)

    assert path == f"{FOLDER}/{NAME}"
    assert cli.commands() == [
        ["create-folder", "--dir-path", FOLDER, "--parent-fid", "0"],
        ["upload", str(local), "--parent-fid", FOLDER_ID],
    ]
    assert seen == {"name": NAME, "text": "[Script Info]\n"}


def test_the_uploads_progress_reaches_the_callback(root, cli, local):
    putting(cli)
    reports = []

    drive(root).put_file(local, FOLDER, lambda sent, total: reports.append((sent, total)))

    size = local.stat().st_size
    assert reports == [(size // 2, size), (size, size)]


def test_no_share_is_made_no_archive_touched_and_the_local_file_left_to_the_caller(root, cli, local):
    putting(cli)

    drive(root).put_file(local, FOLDER)

    assert "share" not in [c[0] for c in cli.commands()]
    assert not root.exists()  # no drive copy built, no archive folder read or made
    assert local.read_text(encoding="utf-8") == "[Script Info]\n"


# ---- failures ----

@pytest.mark.parametrize("answer, message", [
    (upload_failed(32003, "空间不足"), "空间不足"),
    (upload_failed(32004, "单文件超限"), "单文件超限"),
    (upload_failed(-204, "上传失败"), "上传失败"),
], ids=["drive full", "file over the limit", "other"])
def test_a_failed_upload_raises_failed_with_the_cli_message_and_forgets_its_task_record(root, cli, local, answer,
                                                                                       message):
    putting(cli, answer=answer)
    cli.answers["upload delete"] = [UPLOAD_DELETED]

    with pytest.raises(DriveFailed, match=message):
        drive(root).put_file(local, FOLDER)

    assert cli.commands()[-1] == ["upload", "delete", "--record-id", RECORD_ID]
    assert not [c for c in cli.commands() if "resume" in c]


@pytest.mark.parametrize("step, answer, code", [
    ("create-folder", [LAPSED_103], -103),
    ("create-folder", [lapsed_11001("create-folder")], 11001),
    ("upload", upload_failed(11001, "长效Access Token过期，需触发刷新", record=False), 11001),
], ids=["no state", "tokens refused", "while uploading"])
def test_a_lapsed_authorization_while_putting_a_file_raises_lapsed_in_words_of_its_own(root, cli, local, step, answer,
                                                                                      code):
    putting(cli)
    cli.answers[step] = answer
    cli.answers["upload list"] = [result("upload-list", data={"totalCount": 0})]

    with pytest.raises(DriveAuthorizationLapsed) as lapsed:
        drive(root).put_file(local, FOLDER)
    assert str(lapsed.value) == f"夸克网盘的授权已失效（{code}）"  # never the CLI's text: it may hold the device id


@pytest.mark.parametrize("answer, code", [
    ([lapsed_11001("browse")], 11001),
    ([needs_authorization("browse")], -106),
], ids=["tokens refused", "the authorization link"])
def test_a_lapsed_authorization_while_listing_raises_lapsed_in_words_of_its_own(root, cli, answer, code):
    putting(cli)
    cli.answers["browse"] = answer

    with pytest.raises(DriveAuthorizationLapsed) as lapsed:
        drive(root).folder_names(FOLDER)
    assert str(lapsed.value) == f"夸克网盘的授权已失效（{code}）"
    assert DEVICE_ID not in str(lapsed.value)


def test_a_listing_without_its_file_raises_failed(root, cli):
    putting(cli)
    cli.answers["browse"] = [result("browse", data={"total": 0, "hasMore": False, "file_list": []})]

    with pytest.raises(DriveFailed, match="完整列表"):
        drive(root).folder_names(FOLDER)


def test_no_address_from_a_cli_message_reaches_a_failure(root, cli, local):
    putting(cli, answer=upload_failed(-204, f"上传失败，请到 {AUTH_URL} 查看"))
    cli.answers["upload delete"] = [UPLOAD_DELETED]

    with pytest.raises(DriveFailed) as failed:
        drive(root).put_file(local, FOLDER)
    assert "上传失败" in str(failed.value)
    assert "http" not in str(failed.value) and DEVICE_ID not in str(failed.value)


def test_an_upload_without_progress_for_the_idle_time_is_killed_and_failed(root, cli, local):
    putting(cli)
    stuck = FakeProcess([progress(1, 10)], hang=True)
    cli.answers.update({"upload": stuck, "upload delete": [UPLOAD_DELETED], "upload list": [
        line(code=0, msg="", data={"recordId": "other-record", "fileName": "别的.ass", "fileSize": 1, "state": "failed",
                                   "createdAt": 1}, action="upload-list", type="list"),
        line(code=0, msg="", data={"recordId": RECORD_ID, "fileName": NAME, "fileSize": 10, "state": "uploading",
                                   "createdAt": 2}, action="upload-list", type="list"),
        result("upload-list", data={"totalCount": 2})]})

    with pytest.raises(DriveFailed, match="没有进度"):
        drive(root, idle_timeout=0.3).put_file(local, FOLDER)

    assert stuck.killed.is_set()
    assert cli.commands()[-2:] == [["upload", "list"], ["upload", "delete", "--record-id", RECORD_ID]]


# ---- a folder's names ----

def test_a_folders_names_come_from_the_full_listing_not_the_preview(root, cli):
    names = [f"【待校】第{n}话.ass" for n in range(1, 8)]  # more than the preview's five
    putting(cli, files=[(name, 100 + n) for n, name in enumerate(names)])

    found = drive(root).folder_names(FOLDER)

    assert found == names
    assert cli.commands() == [
        ["create-folder", "--dir-path", FOLDER, "--parent-fid", "0"],
        ["browse", "--parent-fid", FOLDER_ID, "--all"],
    ]


def test_a_folder_that_was_missing_is_made_and_has_no_names(root, cli):
    putting(cli)

    assert drive(root).folder_names(FOLDER) == []
    assert cli.commands()[0] == ["create-folder", "--dir-path", FOLDER, "--parent-fid", "0"]
