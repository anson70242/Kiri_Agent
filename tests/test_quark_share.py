"""The drive link of an uploaded drive copy: a private, never-expiring share made through the
fake CLI, saved in the archive, and returned to the pipeline and the `upload` command."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.archive import Archive, Artifact, DriveLink

from .helpers import sample_archive
from .quark_fakes import FILE_ID, PASSCODE, SHARE_URL, SHARED, lapsed_11001, result
from .test_quark_upload import ZIP, cli, drive, root, upload_command, uploading, write_config  # noqa: F401

PATH = f"录播/森野鈴/{ZIP}"
ADDRESS = f"{SHARE_URL}?pwd={PASSCODE}"
OLD = DriveLink("https://pan.quark.cn/s/0ld0ld0ld0ld?pwd=0ldc", "0ldc", PATH,
                datetime(2026, 11, 1, tzinfo=timezone.utc))


def shares(cli):
    return [c for c in cli.commands() if c[0] == "share"]


def test_an_uploaded_drive_copy_is_shared_privately_for_good_and_its_link_saved_in_the_archive(root, cli):
    folder = sample_archive(root)
    uploading(cli)
    cli.answers["share"] = [SHARED]

    copy = drive(root)(Archive.from_folder(folder), "录播/森野鈴", lambda sent, total: None)

    assert copy.path == PATH and copy.link == ADDRESS
    assert shares(cli) == [["share", FILE_ID, "--url-type", "2", "--expired-type", "1"]]
    link = Archive.from_folder(folder).read_drive_link()
    assert (link.url, link.code, link.path) == (ADDRESS, PASSCODE, PATH)
    assert link.made.tzinfo is not None and datetime.now(timezone.utc) - link.made < timedelta(minutes=1)


def test_a_zip_found_on_the_drive_for_an_archive_without_a_link_is_shared_with_the_listings_id(root, cli):
    folder = sample_archive(root)
    uploading(cli, files=[("别的.zip", 5), (ZIP, 123)])
    cli.answers["share"] = [SHARED]

    done = drive(root).upload(Archive.from_folder(folder), "录播/森野鈴")

    assert not done.sent and done.copy.link == ADDRESS
    assert shares(cli) == [["share", "~1AAAA1|BBBB", "--url-type", "2", "--expired-type", "1"]]
    assert Archive.from_folder(folder).read_drive_link().url == ADDRESS


def test_an_archive_with_a_link_whose_zip_is_there_is_left_alone(root, cli):
    folder = sample_archive(root)
    Archive.from_folder(folder).save(Artifact.DRIVE_LINK, OLD)
    uploading(cli, files=[(ZIP, 123)])
    cli.answers["share"] = [SHARED]

    done = drive(root).upload(Archive.from_folder(folder), "录播/森野鈴")

    assert not done.sent and done.copy.link == OLD.url
    assert [c[0] for c in cli.commands()] == ["create-folder", "browse"]
    assert Archive.from_folder(folder).read_drive_link() == OLD


def test_an_archive_with_a_link_whose_zip_is_gone_is_uploaded_and_shared_again(root, cli):
    folder = sample_archive(root)
    Archive.from_folder(folder).save(Artifact.DRIVE_LINK, OLD)
    uploading(cli)
    cli.answers["share"] = [SHARED]

    done = drive(root).upload(Archive.from_folder(folder), "录播/森野鈴")

    assert done.sent and done.copy.link == ADDRESS
    assert Archive.from_folder(folder).read_drive_link().url == ADDRESS


@pytest.mark.parametrize("answer, reason", [
    (result("share", -2101, "分享失败：文件违规"), "分享失败：文件违规"),
    (result("share", data={"pwd_id": "x"}), "没有给出"),
], ids=["refused", "no address"])
def test_a_failed_share_after_an_upload_still_returns_the_path_with_the_reason(root, cli, answer, reason):
    folder = sample_archive(root)
    uploading(cli)
    cli.answers["share"] = [answer]

    done = drive(root).upload(Archive.from_folder(folder), "录播/森野鈴")

    assert done.sent and done.copy.path == PATH and done.copy.link is None and reason in done.copy.reason
    assert not Archive.from_folder(folder).has(Artifact.DRIVE_LINK)
    assert not [c for c in cli.commands() if c[:2] == ["upload", "delete"]]  # the upload did not fail


def test_a_lapsed_authorization_at_the_share_gives_the_uploaded_drive_copy_without_a_link(root, cli):
    folder = sample_archive(root)
    uploading(cli)
    cli.answers["share"] = [lapsed_11001("share")]

    done = drive(root).upload(Archive.from_folder(folder), "录播/森野鈴")

    assert done.sent and done.copy.path == PATH and done.copy.link is None and "授权已失效" in done.copy.reason
    assert not Archive.from_folder(folder).has(Artifact.DRIVE_LINK)


def test_the_command_prints_each_drive_link_or_why_there_is_none(tmp_path, root, cli, capsys):
    cfg = write_config(tmp_path, root)
    sample_archive(root, date="20261105", title="歌枠")
    sample_archive(root, date="20261106", title="雑談")
    uploading(cli)
    cli.answers["share"] = ([SHARED], [result("share", -2101, "分享失败")])

    code, out = upload_command(cfg, capsys, root)

    assert code == 0
    assert out == [
        f"已上传：{ZIP} → {PATH}",
        f"  网盘链接：{ADDRESS}",
        "已上传：[20261106][森野鈴] 雑談.zip → 录播/森野鈴/[20261106][森野鈴] 雑談.zip",
        "  没有网盘链接：分享失败（-2101）",
    ]
