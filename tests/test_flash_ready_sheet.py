"""The source record sheet is told the flash link when "全部处理完成" goes out with it, and no
short download link is made for that live. A recorder stands in for the sheet's module.
Through the pipeline with a fake clock and a fake flash service."""

from __future__ import annotations

from backend.downloader.auto import ShortDownloadLink

from .flash_stage_fakes import EXPIRES, FLASH_LINK, processed_live, ready, run_flash
from .helpers import A
from .test_drive_stage import FakeDrive, step
from .test_source_record_pipeline import Recorder


def short_links(ad) -> list[str]:
    """Make the pipeline's short download links countable: the folders they were made for."""
    made: list[str] = []

    def short(folder: str) -> ShortDownloadLink:
        made.append(folder)
        return ShortDownloadLink(f"/d/Code{len(made):06d}", 1_797_957_000.0)

    ad.download_link = short
    return made


def test_the_sheet_is_told_the_flash_link_and_its_expiry_and_no_short_download_link_is_made(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path)
    ad.source_record = record = Recorder()
    made = short_links(ad)
    run_flash(ad, clock)

    assert record.told == [("processed", A, {
        "date": "20261110", "title": "title v1", "link": FLASH_LINK, "expires": EXPIRES.timestamp(),
        "stream_id": "v1", "flash": True})]
    assert made == []
    assert ready(sent).links[0].url == FLASH_LINK


def test_without_a_public_address_the_flash_link_still_goes_out_and_is_told(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path)
    ad.site_url = ""
    ad.source_record = record = Recorder()
    run_flash(ad, clock)

    assert [link.url for link in ready(sent).links] == [FLASH_LINK]  # no page without the address
    assert [values["link"] for _, _, values in record.told] == [FLASH_LINK]


def test_a_live_with_a_drive_copy_too_is_told_processed_once_with_the_flash_link(tmp_path):
    ad, clock, _, _ = processed_live(tmp_path, folders={"youtube:@a": "录播/A"}, drive=FakeDrive())
    ad.source_record = record = Recorder()
    step(ad, "drive")
    run_flash(ad, clock)

    assert [(what, values["link"]) for what, _, values in record.told] == [
        ("uploaded", "https://drive.example/s/abc?pwd=x1y2"), ("processed", FLASH_LINK)]
