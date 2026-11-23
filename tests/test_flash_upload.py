"""The flash module's command `upload <archive folder>`: one archive's flash transfer by hand,
against the fake flash service, with a clock the test moves."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from backend.archive import Archive, ArtifactMissing, Downloads
from backend.flash.__main__ import main

from .flash_fakes import EXPIRE_TIME, FLASH_LINK, FakeFlashService, fileset
from .helpers import bare_archive, sample_archive

STEM = "[20261105][森野鈴] 歌枠"


@pytest.fixture
def service():
    with FakeFlashService() as fake:
        yield fake


@pytest.fixture
def root(tmp_path):
    return tmp_path / "downloads"


@pytest.fixture
def config(tmp_path, root):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"download": {"output_dir": str(root)}}), encoding="utf-8")
    return cfg


class Clock:
    """Monotonic seconds that move only when the command sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def upload(config, service, folder, capsys, clock=None, **env) -> tuple[int, str]:
    clock = clock or Clock()
    code = main(["upload", str(folder), "--config", str(config)], env=service.env(**env),
                sleep=clock.sleep, clock=clock)
    out = capsys.readouterr()
    return code, out.out + out.err


def staging_is_gone(root) -> bool:
    return not Downloads(root).flash_staging.exists()


def test_an_archive_is_uploaded_as_one_flash_transfer_and_its_flash_link_is_noted_and_printed(
        service, config, root, capsys):
    folder = sample_archive(root, raw_chat={"raw": True})
    service.progress = [fileset(0, 1000), fileset(500, 1000, done=2), fileset(1000, 1000, done=5, finished=True)]

    code, out = upload(config, service, folder, capsys, FLASH_DOWNLOADS_PATH="/mnt/dl")

    assert code == 0
    assert len(service.created) == 1
    assert service.created[0]["name"] == STEM
    sent = [path.rsplit("/", 1)[-1] for path in service.created[0]["files"]]
    assert sorted(sent) == sorted([f"{STEM}.mp4", f"{STEM}.chat_parsed.json", f"{STEM}.zh.srt", f"{STEM}.ja.srt",
                                   f"{STEM}.highlights.xlsx"])
    assert f"/mnt/dl/森野鈴/20261105/歌枠/{STEM}.mp4" in service.created[0]["files"]
    link = Archive.from_folder(folder).read_flash_link()
    assert link.url == FLASH_LINK
    assert link.expires == datetime.fromtimestamp(EXPIRE_TIME, timezone.utc)
    assert link.made <= datetime.now(timezone.utc) and link.made.tzinfo is not None
    assert out.strip().splitlines()[-1].endswith(FLASH_LINK)
    assert "50%" in out
    assert staging_is_gone(root)


def test_the_archive_may_be_named_by_its_folder_under_the_downloads_folder(service, config, root, capsys):
    folder = sample_archive(root)

    code, _ = upload(config, service, "森野鈴/20261105/歌枠", capsys)

    assert code == 0
    assert Archive.from_folder(folder).read_flash_link().url == FLASH_LINK


def test_the_command_asks_for_progress_until_the_flash_transfer_is_finished(service, config, root, capsys):
    folder = sample_archive(root)
    service.progress = [fileset(0, 1000)] * 5 + [fileset(1000, 1000, done=5, finished=True)]
    clock = Clock()

    code, _ = upload(config, service, folder, capsys, clock)

    assert code == 0
    assert len(clock.sleeps) == 5
    assert sum(1 for a in service.actions if a["action"] == "get_fileset_info") == 6


def test_a_failed_file_ends_the_command_with_an_error_and_notes_no_flash_link(service, config, root, capsys):
    folder = sample_archive(root)
    service.progress = [fileset(500, 1000, done=4, failed=1, finished=True)]

    code, out = upload(config, service, folder, capsys)

    assert code == 1
    assert "失败" in out
    with pytest.raises(ArtifactMissing):
        Archive.from_folder(folder).read_flash_link()
    assert staging_is_gone(root)
    assert FLASH_LINK not in out


def test_an_upload_that_does_not_move_for_thirty_minutes_ends_the_command_with_an_error(service, config, root, capsys):
    folder = sample_archive(root)
    service.progress = [fileset(0, 1000), fileset(300, 1000), fileset(300, 1000)]
    clock = Clock()

    code, out = upload(config, service, folder, capsys, clock)

    assert code == 1
    assert "30 分钟" in out
    assert 30 * 60 <= clock.t <= 30 * 60 + max(clock.sleeps)
    with pytest.raises(ArtifactMissing):
        Archive.from_folder(folder).read_flash_link()
    assert staging_is_gone(root)


def test_an_offline_account_ends_the_command_with_an_error_and_starts_nothing(service, config, root, capsys):
    folder = sample_archive(root)
    service.online = False

    code, out = upload(config, service, folder, capsys)

    assert code == 1
    assert "掉线" in out
    assert service.created == []
    assert staging_is_gone(root)


def test_a_start_the_flash_service_refuses_ends_the_command_with_its_words(service, config, root, capsys):
    folder = sample_archive(root)
    service.answers["create_flash_task"] = lambda params: {"data": None, "message": "文件不存在"}

    code, out = upload(config, service, folder, capsys)

    assert code == 1
    assert "文件不存在" in out
    assert staging_is_gone(root)


def test_a_folder_that_is_no_archive_is_refused(service, config, root, capsys):
    (root / "nothing").mkdir(parents=True)

    code, out = upload(config, service, root / "nothing", capsys)

    assert code == 1
    assert service.created == []


def test_an_archive_without_a_video_is_refused(service, config, root, capsys):
    folder = bare_archive(root, chat=[])

    code, out = upload(config, service, folder, capsys)

    assert code == 1
    assert "影片" in out
    assert service.created == []


def test_without_a_flash_service_the_command_says_so(config, root, capsys):
    folder = sample_archive(root)

    code = main(["upload", str(folder), "--config", str(config)], env={})

    assert code == 2
    assert "FLASH_ACTIONS_URL" in capsys.readouterr().err


def test_the_flash_link_is_printed_but_in_no_log_line(service, config, root, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    folder = sample_archive(root)

    code, out = upload(config, service, folder, capsys)

    assert code == 0 and FLASH_LINK in out
    assert FLASH_LINK not in caplog.text
