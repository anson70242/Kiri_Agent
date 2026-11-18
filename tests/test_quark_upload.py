"""Uploading an archive's drive copy through the Quark module, by the call the pipeline will
use and by the `upload` command; run through a fake CLI on real archive folders."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from backend import quark
from backend.archive import Archive, Downloads
from backend.drive import DriveAuthorizationLapsed, DriveFailed

from .helpers import sample_archive
from .quark_fakes import (AUTH_URL, DEVICE_ID, FOLDER_ID, LAPSED_103, NOISE, RECORD_ID, SHARED, SHARE_URL, UPLOAD_DELETED,
                          FakeCli, FakeProcess, browse, create_folder, lapsed_11001, line, needs_authorization, progress,
                          result, upload_failed, uploaded)

STEM = "[20261105][柊優花] 歌枠"
ZIP = f"{STEM}.zip"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "downloads"


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCli({})
    monkeypatch.setattr(quark, "run_cli", fake)
    return fake


def drive(root, **kw):
    return quark.Quark(Path("/data/quark/scripts/quark-drive.cjs"), root, **kw)


def uploading(cli, files=(), answer=None):
    """The fake CLI: the folder made, listed with `files`, and the upload answered with
    `answer` (a successful upload of what it is given by default). Returns what the upload
    saw: the zip's name, size and contents while it was being uploaded."""
    seen = {}

    def upload(argv, cwd):
        zip_path = Path(argv[3])
        seen.update(name=zip_path.name, size=zip_path.stat().st_size, names=zipfile.ZipFile(zip_path).namelist())
        return answer if answer is not None else uploaded(zip_path.name, zip_path.stat().st_size)

    cli.answers.update({"create-folder": NOISE + [create_folder()], "browse": browse(list(files)), "upload": upload,
                        "upload delete": [UPLOAD_DELETED], "share": [SHARED]})
    return seen


def no_zip_left(root):
    return not list(root.rglob("*.zip"))


def built(root):
    """Whether a drive copy was ever built under the downloads root (its place made)."""
    return Downloads(root).drive_copies.exists()


# ---- the call ----

def test_a_drive_copy_is_uploaded_into_the_folder_made_from_the_top_of_the_drive(root, cli):
    folder = sample_archive(root)
    seen = uploading(cli)

    done = drive(root).upload(Archive.from_folder(folder), "录播/柊優花")

    assert done.sent
    assert done.copy.path == f"录播/柊優花/{ZIP}" and done.copy.link.startswith(SHARE_URL)
    assert cli.commands() == [
        ["create-folder", "--dir-path", "录播/柊優花", "--parent-fid", "0"],
        ["browse", "--parent-fid", FOLDER_ID, "--all"],
        ["upload", cli.commands()[2][1], "--parent-fid", FOLDER_ID],
        cli.commands()[3],
    ]
    assert seen["name"] == ZIP and f"{STEM}.highlights.xlsx" in seen["names"]
    assert no_zip_left(root)


@pytest.mark.parametrize("size", [10_000, 123], ids=["one size", "another size"])
def test_a_zip_with_the_same_name_on_the_drive_whatever_its_size_is_neither_built_nor_uploaded(root, cli, size):
    folder = sample_archive(root)
    uploading(cli, files=[("别的.zip", 5), (ZIP, size)])

    done = drive(root).upload(Archive.from_folder(folder), "录播/柊優花")

    assert not done.sent and done.copy.path == f"录播/柊優花/{ZIP}"
    assert [c[0] for c in cli.commands()] == ["create-folder", "browse", "share"]
    assert not built(root)


def test_the_uploads_progress_reaches_the_callback(root, cli):
    folder = sample_archive(root)
    seen = uploading(cli)
    reports = []

    drive(root).upload(Archive.from_folder(folder), "录播/柊優花", lambda sent, total: reports.append((sent, total)))

    size = seen["size"]
    assert reports[-2:] == [(size // 2, size), (size, size)]


def test_the_pipeline_gets_the_drive_copy_from_the_drive_as_a_call(root, cli):
    folder = sample_archive(root)
    uploading(cli)

    copy = drive(root)(Archive.from_folder(folder), "录播/柊優花", lambda sent, total: None)

    assert copy.path == f"录播/柊優花/{ZIP}" and copy.link.startswith(SHARE_URL)


# ---- failures ----

@pytest.mark.parametrize("answer, message", [
    (upload_failed(32003, "空间不足"), "空间不足"),
    (upload_failed(32004, "单文件超限"), "单文件超限"),
    (upload_failed(-204, "上传失败"), "上传失败"),
], ids=["drive full", "file over the limit", "other"])
def test_a_failed_upload_raises_failed_with_the_cli_message_and_forgets_its_task_record(root, cli, answer, message):
    folder = sample_archive(root)
    uploading(cli, answer=answer)

    with pytest.raises(DriveFailed, match=message):
        drive(root).upload(Archive.from_folder(folder), "录播/柊優花")

    assert cli.commands()[-1] == ["upload", "delete", "--record-id", RECORD_ID]
    assert not [c for c in cli.commands() if "resume" in c]
    assert no_zip_left(root)


@pytest.mark.parametrize("step, answer, code", [
    ("create-folder", [LAPSED_103], -103),
    ("create-folder", [lapsed_11001("create-folder")], 11001),
    ("browse", [lapsed_11001("browse")], 11001),
    ("browse", [needs_authorization("browse")], -106),
    ("upload", upload_failed(11001, "长效Access Token过期，需触发刷新", record=False), 11001),
], ids=["no state", "tokens refused", "while listing", "the authorization link", "while uploading"])
def test_a_lapsed_authorization_raises_lapsed_in_words_of_its_own(root, cli, step, answer, code):
    folder = sample_archive(root)
    uploading(cli)
    cli.answers[step] = answer
    cli.answers["upload list"] = [result("upload-list", data={"totalCount": 0})]

    with pytest.raises(DriveAuthorizationLapsed) as lapsed:
        drive(root).upload(Archive.from_folder(folder), "录播/柊優花")
    assert str(lapsed.value) == f"夸克网盘的授权已失效（{code}）"  # never the CLI's text: it may hold the device id
    assert no_zip_left(root)
    assert built(root) == (step == "upload")  # found before the zip is built, when it can be


def test_no_address_from_a_cli_message_reaches_a_failure(root, cli):
    folder = sample_archive(root)
    uploading(cli, answer=upload_failed(-204, f"上传失败，请到 {AUTH_URL} 查看"))

    with pytest.raises(DriveFailed) as failed:
        drive(root).upload(Archive.from_folder(folder), "录播/柊優花")
    assert "上传失败" in str(failed.value)
    assert "http" not in str(failed.value) and DEVICE_ID not in str(failed.value)


def test_an_upload_without_progress_for_the_idle_time_is_killed_and_failed(root, cli):
    folder = sample_archive(root)
    uploading(cli)
    stuck = FakeProcess([progress(1, 10)], hang=True)
    cli.answers["upload"] = stuck
    cli.answers["upload list"] = [
        line(code=0, msg="", data={"recordId": "other-record", "fileName": "别的.zip", "fileSize": 1, "state": "failed",
                                   "createdAt": 1}, action="upload-list", type="list"),
        line(code=0, msg="", data={"recordId": RECORD_ID, "fileName": ZIP, "fileSize": 10, "state": "uploading",
                                   "createdAt": 2}, action="upload-list", type="list"),
        result("upload-list", data={"totalCount": 2})]

    with pytest.raises(DriveFailed, match="没有进度"):
        drive(root, idle_timeout=0.3).upload(Archive.from_folder(folder), "录播/柊優花")

    assert stuck.killed.is_set()
    assert cli.commands()[-2:] == [["upload", "list"], ["upload", "delete", "--record-id", RECORD_ID]]
    assert no_zip_left(root)


def test_an_archive_without_video_uploads_nothing(root, cli):
    folder = sample_archive(root, video=None)

    with pytest.raises(DriveFailed, match="没有影片"):
        drive(root).upload(Archive.from_folder(folder), "录播/柊優花")
    assert cli.calls == []


# ---- the command ----

def write_config(tmp_path, root, streamers=()):
    script = tmp_path / "cli" / "quark-drive.cjs"
    script.parent.mkdir()
    script.write_text("// the CLI", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "streamers.json").write_text(json.dumps(list(streamers), ensure_ascii=False), encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"drive": {"quark_cli": str(script)}, "download": {"output_dir": str(root)},
                               "monitor": {"streamers_file": "data/streamers.json"}}), encoding="utf-8")
    return cfg


def upload_command(cfg, capsys, *args):
    code = quark.main(["upload", *map(str, args), "--config", str(cfg)])
    return code, capsys.readouterr().out.splitlines()


def test_the_command_uploads_every_archive_in_a_folder_and_says_what_became_of_each(tmp_path, root, cli, capsys):
    cfg = write_config(tmp_path, root)
    sample_archive(root, date="20261105", title="歌枠")
    sample_archive(root, date="20261106", title="雑談")
    sample_archive(root, date="20261107", title="没有影片", video=None)
    sample_archive(root, date="20261108", title="坏了")
    seen = uploading(cli, files=[("[20261106][柊優花] 雑談.zip", 123)])
    upload = cli.answers["upload"]
    cli.answers["upload"] = lambda argv, cwd: (upload_failed(-204, "网络超时") if "坏了" in argv[3] else upload(argv, cwd))

    code, out = upload_command(cfg, capsys, root / "柊優花")

    assert code == 1  # one failed
    assert out == [
        f"已上传：{ZIP} → 录播/柊優花/{ZIP}",
        f"  网盘链接：{SHARE_URL}?pwd=Ab3d",
        "已在网盘：[20261106][柊優花] 雑談.zip → 录播/柊優花/[20261106][柊優花] 雑談.zip",
        f"  网盘链接：{SHARE_URL}?pwd=Ab3d",
        "跳过：柊優花/20261107/没有影片（没有影片）",
        "失败：[20261108][柊優花] 坏了.zip：网络超时（-204）",
    ]
    assert seen["name"] == ZIP


def test_the_command_takes_one_archive_folder_and_the_folder_given(tmp_path, root, cli, capsys):
    cfg = write_config(tmp_path, root)
    folder = sample_archive(root)
    uploading(cli)

    code, out = upload_command(cfg, capsys, folder, "--folder", "直播录像/优花")

    assert code == 0
    assert out == [f"已上传：{ZIP} → 直播录像/优花/{ZIP}", f"  网盘链接：{SHARE_URL}?pwd=Ab3d"]
    assert cli.commands()[0][:3] == ["create-folder", "--dir-path", "直播录像/优花"]


@pytest.mark.parametrize("streamers, expected", [
    ([{"name": "柊優花", "platform": "youtube", "channel": "@a", "drive": True, "drive_folder": "直播录像/优花"}],
     "直播录像/优花"),
    ([{"name": "柊優花", "platform": "youtube", "channel": "@a"},
      {"name": "柊優花", "platform": "twitch", "channel": "b", "drive_folder": "直播录像/Twitch"}], "直播录像/Twitch"),
    ([{"name": "柊優花", "platform": "youtube", "channel": "@a"}], "录播/柊優花"),
    ([{"name": "别人", "platform": "youtube", "channel": "@b", "drive_folder": "别人"}], "录播/柊優花"),
], ids=["the roster's folder", "the entry with a folder", "the roster's blank folder", "no roster entry"])
def test_without_a_folder_the_command_takes_the_roster_entry_named_like_the_creator(tmp_path, root, cli, capsys,
                                                                                   streamers, expected):
    cfg = write_config(tmp_path, root, streamers)
    folder = sample_archive(root)
    uploading(cli)

    upload_command(cfg, capsys, folder)

    assert cli.commands()[0][:3] == ["create-folder", "--dir-path", expected]


def test_the_command_stops_when_the_authorization_lapsed(tmp_path, root, cli, capsys):
    cfg = write_config(tmp_path, root)
    sample_archive(root, date="20261105")
    sample_archive(root, date="20261106")
    uploading(cli)
    cli.answers["create-folder"] = [LAPSED_103]

    code, out = upload_command(cfg, capsys, root)

    assert code == 1
    assert len(out) == 2 and out[0].startswith("失败：") and "授权" in out[1] and "backend.quark login" in out[1]
    assert len(cli.calls) == 1


def test_the_command_skips_an_archive_still_being_downloaded(tmp_path, root, cli, capsys):
    cfg = write_config(tmp_path, root)
    folder = sample_archive(root)
    (folder / f"{STEM}.live_chat.json.part").write_text("{", encoding="utf-8")  # its chat still coming

    code, out = upload_command(cfg, capsys, folder)

    assert code == 0
    assert out == ["跳过：柊優花/20261105/歌枠（还在下载）"]
    assert cli.calls == []
