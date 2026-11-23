"""The command `upload <archive folder>`: the folder is found through the Archive module, inside
the downloads folder only, and an offline flash account ends the command before any file is
prepared or any video cut. Against the fake flash service."""

from __future__ import annotations

import pytest

import backend.flash.__main__ as command

from .helpers import sample_archive
from .test_flash_upload import config, root, service, staging_is_gone, upload  # noqa: F401 - fixtures


@pytest.mark.parametrize("reference", ["../outside/森野鈴/20261105/歌枠", "{outside}"])
def test_a_folder_outside_the_downloads_folder_is_refused(service, config, root, tmp_path, capsys,  # noqa: F811
                                                          reference):
    outside = sample_archive(tmp_path / "outside")
    root.mkdir()

    code, out = upload(config, service, reference.format(outside=outside), capsys)

    assert code == 1
    assert "不是下载目录里的录播文件夹" in out
    assert service.created == []


def test_an_archive_folder_given_by_its_path_inside_the_downloads_folder_is_uploaded(service, config, root,  # noqa: F811
                                                                                    capsys):
    folder = sample_archive(root)

    code, _ = upload(config, service, folder / ".." / ".." / ".." / "森野鈴" / "20261105" / "歌枠", capsys)

    assert code == 0 and len(service.created) == 1


def test_an_offline_account_ends_the_command_before_anything_is_prepared_or_cut(service, config, root, capsys,  # noqa: F811
                                                                               monkeypatch):
    sample_archive(root)
    service.online = False
    prepared: list[object] = []
    monkeypatch.setattr(command, "needs_cutting", lambda archive: True)
    monkeypatch.setattr(command, "prepare", lambda *a, **kw: prepared.append(a))

    code, out = upload(config, service, "森野鈴/20261105/歌枠", capsys)

    assert code == 1
    assert "掉线" in out and "切" not in out
    assert prepared == [] and service.created == []
    assert staging_is_gone(root)
