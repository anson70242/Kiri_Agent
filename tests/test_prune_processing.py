"""Pruning leaves alone an archive that is still being processed (it has a job in the queue,
at any stage): on real folders in a temporary downloads tree, and through the automatic
download with fake stages."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.archive import Downloads
from backend.downloader import auto as auto_mod
from backend.downloader.auto import AutoDownloader
from backend.downloader.base import DownloadResult, DownloadSettings, VideoInfo
from backend.notify import EventKind, StreamEvent

from .helpers import A, bare_archive, later, live, sample_archive


def dated_archives(root: Path, creator: str, dates: list[str]) -> list[Path]:
    """One archive folder per date (titles t0, t1, ...), each folder's time one second later."""
    folders = []
    for i, date in enumerate(dates):
        f = sample_archive(root, creator, date, f"t{i}")
        os.utime(f, (1000 + i, 1000 + i))
        folders.append(f)
    return folders


# ---- on real folders ----

def test_an_archive_still_being_processed_survives_a_prune_that_would_delete_it(tmp_path):
    root = tmp_path / "downloads"
    first, new = dated_archives(root, "A", ["20261101", "20261102"])

    assert Downloads(root).prune(new, keep=1, processing=[first]) == []
    assert first.exists() and new.exists()


def test_an_archive_spared_while_processed_is_not_counted_as_deleted(tmp_path):
    root = tmp_path / "downloads"
    first, middle, last, new = dated_archives(root, "A", ["20261101", "20261102", "20261103", "20261104"])
    (other,) = dated_archives(root, "B", ["20261001"])  # another creator: never counted or touched

    removed = Downloads(root).prune(new, keep=2, processing=[first, other])

    assert removed == [middle]  # beyond the keep count by one, after sparing the first
    assert first.exists() and last.exists() and new.exists() and other.exists()


def test_once_processed_the_spared_archive_goes_at_the_next_prune(tmp_path):
    root = tmp_path / "downloads"
    first, second = dated_archives(root, "A", ["20261101", "20261102"])
    assert Downloads(root).prune(second, keep=1, processing=[first]) == []
    (third,) = dated_archives(root, "A", ["20261103"])

    assert Downloads(root).prune(third, keep=1) == [first, second]
    assert third.exists()


# ---- through the automatic download ----

class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class FakeDL:
    """Finds each live's replay at the live's own address, ready to download."""

    def find_replay(self, url, channel, live_id, started_at):
        return url

    def info(self, url):
        sid = url.rsplit("/", 1)[-1]
        return VideoInfo("youtube", sid, url, f"title {sid}", "@a", "A", "20261110", live_status="was_live")


@pytest.fixture
def ad(tmp_path, monkeypatch):
    """Automatic download for A (保留数量 1), with fake stages that always succeed.
    Each live is downloaded into its own archive, one day after the one before."""
    dates = iter(f"202611{d:02d}" for d in range(1, 31))

    def fake_download(url, settings, streamers, info=None, progress=None):
        folder = bare_archive(settings.output_dir, "A", next(dates), info.id, stem=info.id, video=b"x")
        return DownloadResult(info, folder, video=folder / f"{info.id}.mp4")

    def fake_analyze(video, streamer, title):
        table = video.with_suffix(".highlights.json")
        table.write_text(json.dumps({"rows": []}), encoding="utf-8")
        return table

    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: FakeDL())
    monkeypatch.setattr(auto_mod, "download", fake_download)
    settings = DownloadSettings(output_dir=tmp_path / "dl", pending_file=tmp_path / "pending.json")
    sent: list[StreamEvent] = []
    ad = AutoDownloader(
        settings, [A], {A.id: 1}, tmp_path / "queue.json",
        notify=sent.append, clock=Clock(),
        transcribe=lambda video, **kw: None,
        analyze=fake_analyze,
        translate=lambda video: None,
        upload=lambda table, sheet, streamer, title: "https://docs.example/sheet",
    )
    ad.uploads = {A.id: "sheet-a"}
    ad.sent = sent
    return ad


def end_and_download(ad, sid):
    """A live of A ends and is downloaded; returns its archive folder."""
    ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid=sid), later(60)))
    job = ad.next_due("download")
    outcome = ad.attempt(job)
    ad.finish(job, outcome)
    return outcome.result.output_dir


def step(ad, stage):
    job = ad.next_due(stage)
    ad.finish(job, ad.attempt(job))


def last_downloaded(ad) -> StreamEvent:
    return [e for e in ad.sent if e.kind is EventKind.DOWNLOADED][-1]


# The steps that leave the first live's only job at the given stage.
AT_STAGE = {
    "transcribe": [],
    "analyze": ["transcribe", "translate"],
    "translate": ["transcribe", "analyze", "upload"],
    "upload": ["transcribe", "translate", "analyze"],
}


@pytest.mark.parametrize("stage", list(AT_STAGE))
def test_an_archive_with_a_job_left_survives_the_next_download_of_its_streamer(ad, stage):
    first = end_and_download(ad, sid="v1")
    for s in AT_STAGE[stage]:
        step(ad, s)
    assert {j.stage for j in ad.load()} == {stage}

    second = end_and_download(ad, sid="v2")

    assert first.exists() and second.exists()
    assert "清理" not in last_downloaded(ad).detail  # nothing was really deleted


def test_once_its_jobs_are_over_the_archive_goes_at_the_next_prune(ad):
    first = end_and_download(ad, sid="v1")
    second = end_and_download(ad, sid="v2")
    while job := next((j for j in ad.load() if j.status.stream_id == "v1"), None):
        step(ad, job.stage)

    third = end_and_download(ad, sid="v3")

    assert not first.exists()
    assert second.exists() and third.exists()  # the second still has a job left
    assert f"删除了 1 个旧档（{first.name}）" in last_downloaded(ad).detail
