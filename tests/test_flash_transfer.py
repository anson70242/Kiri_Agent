"""The flash service client starts a flash transfer and reads its progress, against the fake
flash service."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from backend.flash.client import VALIDITY_SECONDS, FlashClient, FlashFailed, FlashSettings, FlashUnavailable

from .flash_fakes import EXPIRE_TIME, FILESET_ID, FLASH_LINK, FakeFlashService, closed_port, fileset

NAME = "[20261122][森野鈴] 标题"


@pytest.fixture
def service():
    with FakeFlashService() as fake:
        yield fake


@pytest.fixture
def downloads(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    return folder


def files(downloads):
    folder = downloads / "森野鈴" / "20261122" / "标题"
    return [folder / f"{NAME}.mp4", folder / f"{NAME}.chat_parsed.json"]


# ---- starting ----

def test_a_flash_transfer_is_started_from_its_files_as_the_flash_service_sees_them_and_its_name(service, downloads):
    client = FlashClient(service.settings(FLASH_DOWNLOADS_PATH="/mnt/dl"), downloads)

    client.start(files(downloads), NAME)

    assert service.created == [{"files": [f"/mnt/dl/森野鈴/20261122/标题/{NAME}.mp4",
                                          f"/mnt/dl/森野鈴/20261122/标题/{NAME}.chat_parsed.json"],
                                "name": NAME, "validitySeconds": 1209600}]


def test_a_started_flash_transfer_answers_its_id_its_flash_link_and_its_expiry(service, downloads):
    started = FlashClient(service.settings(), downloads).start(files(downloads), NAME)

    assert started.id == FILESET_ID
    assert started.link == FLASH_LINK
    assert started.expires == datetime.fromtimestamp(EXPIRE_TIME, timezone.utc)


def test_the_validity_is_fourteen_days():
    assert VALIDITY_SECONDS == 14 * 24 * 3600


@pytest.mark.parametrize("expire_time", [EXPIRE_TIME * 1000, str(EXPIRE_TIME)])
def test_an_expiry_in_milliseconds_or_as_text_is_read_too(service, downloads, expire_time):
    original = service.answers["create_flash_task"]

    def create(params):
        answer = original(params)
        answer["data"]["createFlashTransferResult"]["expireTime"] = expire_time
        return answer

    service.answers["create_flash_task"] = create

    started = FlashClient(service.settings(), downloads).start(files(downloads), NAME)

    assert started.expires == datetime.fromtimestamp(EXPIRE_TIME, timezone.utc)


def test_without_a_readable_expiry_the_flash_link_works_for_the_validity_sent(service, downloads):
    service.answers["create_flash_task"] = lambda params: {"data": {"createFlashTransferResult": {
        "fileSetId": FILESET_ID, "shareLink": FLASH_LINK, "expireTime": None}}}
    before = datetime.now(timezone.utc)

    started = FlashClient(service.settings(), downloads).start(files(downloads), NAME)

    assert before.timestamp() + 1209600 <= started.expires.timestamp() <= datetime.now(timezone.utc).timestamp() + 1209600


@pytest.mark.parametrize("result", [{}, {"shareLink": FLASH_LINK}, {"fileSetId": FILESET_ID}, None])
def test_a_start_that_answers_no_id_or_no_link_is_a_failure(service, downloads, result):
    service.answers["create_flash_task"] = lambda params: {"data": {"createFlashTransferResult": result}}

    with pytest.raises(FlashFailed):
        FlashClient(service.settings(), downloads).start(files(downloads), NAME)


def test_a_start_the_flash_service_refuses_is_a_failure_with_its_words(service, downloads):
    service.answers["create_flash_task"] = lambda params: {"data": None, "message": "文件不存在"}

    with pytest.raises(FlashFailed, match="文件不存在"):
        FlashClient(service.settings(), downloads).start(files(downloads), NAME)


def test_a_file_outside_the_downloads_folder_is_a_failure_and_nothing_is_started(service, downloads, tmp_path):
    with pytest.raises(FlashFailed):
        FlashClient(service.settings(), downloads).start([tmp_path / "elsewhere.mp4"], NAME)
    assert service.created == []


def test_starting_while_the_account_is_offline_waits_and_starts_nothing(service, downloads):
    service.online = False

    with pytest.raises(FlashUnavailable):
        FlashClient(service.settings(), downloads).start(files(downloads), NAME)
    assert service.created == []


def test_starting_with_an_unreachable_flash_service_waits(downloads):
    settings = FlashSettings.from_env({"FLASH_ACTIONS_URL": f"ws://127.0.0.1:{closed_port()}"})

    with pytest.raises(FlashUnavailable):
        FlashClient(settings, downloads).start(files(downloads), NAME)


# ---- progress ----

def test_progress_while_uploading(service, downloads):
    service.progress = [fileset(300, 1000, done=1)]

    progress = FlashClient(service.settings(), downloads).progress(FILESET_ID)

    assert (progress.sent, progress.total, progress.done, progress.failed, progress.finished) == (300, 1000, 1, 0, False)
    assert service.actions[-1]["params"] == {"fileset_id": FILESET_ID}


def test_progress_when_finished(service, downloads):
    service.progress = [fileset(1000, 1000, done=3, finished=True)]

    progress = FlashClient(service.settings(), downloads).progress(FILESET_ID)

    assert (progress.sent, progress.total, progress.done, progress.failed, progress.finished) == (1000, 1000, 3, 0, True)


def test_progress_reports_a_failed_file(service, downloads):
    service.progress = [fileset(1000, 1000, done=2, failed=1, finished=True)]

    assert FlashClient(service.settings(), downloads).progress(FILESET_ID).failed == 1


def test_progress_of_an_unknown_flash_transfer_is_a_failure_with_the_flash_services_words(service, downloads):
    with pytest.raises(FlashFailed, match="文件集不存在"):
        FlashClient(service.settings(), downloads).progress("another-id")


def test_progress_without_a_file_set_is_a_failure(service, downloads):
    service.answers["get_fileset_info"] = lambda params: {"data": {}}

    with pytest.raises(FlashFailed):
        FlashClient(service.settings(), downloads).progress(FILESET_ID)


def test_progress_while_the_account_is_offline_waits(service, downloads):
    service.online = False

    with pytest.raises(FlashUnavailable):
        FlashClient(service.settings(), downloads).progress(FILESET_ID)


# ---- nothing of a flash link in the log ----

def test_the_flash_link_is_in_no_log_line_and_not_in_the_started_transfers_text(service, downloads, caplog):
    caplog.set_level(logging.DEBUG)
    client = FlashClient(service.settings(), downloads)

    started = client.start(files(downloads), NAME)
    client.progress(started.id)

    assert FLASH_LINK not in caplog.text
    assert FLASH_LINK not in repr(started) and FLASH_LINK not in str(started)
