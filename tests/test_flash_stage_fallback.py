"""A flash transfer that cannot be made in the pipeline's tries is dropped, and "全部处理完成"
goes out as for a live without one: the short download link and the page, and the source record sheet is told the
short download link. Through the pipeline with a fake clock and a fake flash service."""

from __future__ import annotations

import pytest

from backend.archive import Archive, ArtifactMissing, Downloads
from backend.flash.client import FlashFailed, FlashUnavailable, Progress
from backend.notify import EventKind

from .flash_stage_fakes import FakeFlash, processed_live, ready, run_flash, uploading
from .test_drive_stage import kinds, ready_with_the_sites_links
from .test_flash_ready_sheet import short_links
from .test_source_record_pipeline import Recorder

FAILURES = {  # the fake flash service of each way to fail, at every try (a look's answer repeats)
    "the start is refused": lambda: FakeFlash(start=[FlashFailed("闪传服务拒绝了")] * 3),
    "the account is offline at the start": lambda: FakeFlash(start=[FlashUnavailable("闪传账号掉线")] * 3),
    "the account goes offline while uploading": lambda: FakeFlash(uploading(), FlashUnavailable("闪传账号掉线")),
    "a look at the progress fails": lambda: FakeFlash(uploading(), FlashFailed("闪传服务没有给出闪传的进度")),
    "a file fails": lambda: FakeFlash(Progress(100, 100, done=4, failed=1, finished=True)),
    "a file fails while uploading": lambda: FakeFlash(Progress(50, 100, done=0, failed=1, finished=False)),
    "a surprise": lambda: FakeFlash(start=[RuntimeError("boom")] * 3),
}


@pytest.mark.parametrize("fake", FAILURES.values(), ids=FAILURES.keys())
def test_a_flash_transfer_that_fails_at_every_try_is_dropped_and_ready_goes_out_with_the_short_download_link(
        tmp_path, fake):
    flash = fake()
    staged: list[bool] = []
    flash.during_start = lambda files: staged.append(any(".flash" in str(f) for f in files))
    ad, clock, sent, folder = processed_live(tmp_path, flash, full=True)
    ad.source_record = record = Recorder()
    made = short_links(ad)
    run_flash(ad, clock)

    assert ad.load() == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    assert [label for _, label in ((x.url, x.label) for x in ready(sent).links)] == [
        "全部下载（影片、弹幕、中日字幕 SRT）", "录播页面"]
    assert ready(sent).links[0].url == "https://kiri.example/d/Code000001" and made == ["A/20261110/歌枠"]
    assert "闪传" not in ready(sent).detail
    ((_, _, values),) = record.told
    assert values["link"] == "https://kiri.example/d/Code000001" and "flash" not in values
    with pytest.raises(ArtifactMissing):
        Archive.from_folder(folder).read_flash_link()
    assert staged == [True] * 3  # each try staged the subtitles and the sheet, and they are gone again
    assert not Downloads(ad.settings.output_dir).flash_staging.exists()


def test_a_flash_job_whose_flash_service_is_gone_is_dropped_with_ready_as_without_one(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path)
    ad.flash = None  # .env lost the flash service, then a restart
    run_flash(ad, clock)

    assert ad.load() == []
    ready_with_the_sites_links(sent)
