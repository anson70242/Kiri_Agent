"""Auto-download after a stream ends: queue, retries, failure messages, retention."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.downloader import auto as auto_mod
from backend.downloader.auto import AutoDownloader, Outcome, load_auto
from backend.downloader.base import DownloadError, DownloadResult, DownloadSettings, VideoInfo
from backend.downloader.pending import PendingChats
from backend.downloader.twitch import TwitchDownloader
from backend.downloader.youtube import YoutubeDownloader
from backend.notify import EventKind, LiveNotifier, StreamEvent
from backend.notify.discord import DiscordNotifier

from .helpers import A, B, T0, later, live, off

HOUR = 3600


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def make(tmp_path, keep=None, **kw):
    clock = Clock()
    sent: list[StreamEvent] = []
    settings = DownloadSettings(output_dir=tmp_path / "dl", pending_file=tmp_path / "pending.json")
    ad = AutoDownloader(
        settings, [A, B], keep or {A.id: 2}, tmp_path / "queue.json",
        retry_interval=900, give_up=12 * HOUR, max_errors=3,
        notify=sent.append, clock=clock, **kw,
    )
    return ad, clock, sent


def ended(s=A, sid="v1"):
    return StreamEvent(EventKind.OFFLINE, live(s, sid=sid), later(60))


# ---- queue ----

def test_only_enabled_streamers_are_queued_once(tmp_path):
    ad, _, _ = make(tmp_path)
    ad.on_ended(ended(A))
    ad.on_ended(ended(A))  # same broadcast again
    ad.on_ended(ended(B, "b1"))  # not enabled
    jobs = ad.load()
    assert [j.status.stream_id for j in jobs] == ["v1"]
    assert jobs[0].status.started_at == T0  # survives the JSON round trip


def test_waits_then_gives_up_with_message(tmp_path):
    ad, clock, sent = make(tmp_path)
    ad.on_ended(ended())
    job = ad.next_due()
    ad.finish(job, Outcome("wait", "stream is post_live"))
    assert ad.next_due() is None and sent == []
    clock.t += 900
    job = ad.next_due()
    assert job is not None and job.attempts == 1
    clock.t += 12 * HOUR
    ad.finish(job, Outcome("wait", "stream is post_live"))
    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
    assert "post_live" in sent[0].detail


def test_errors_give_up_after_max_with_cookie_hint(tmp_path):
    ad, clock, sent = make(tmp_path)
    ad.on_ended(ended())
    for _ in range(3):
        job = ad.next_due()
        ad.finish(job, Outcome("error", "Join this channel to get access to members-only content"))
        clock.t += 900
    assert ad.load() == []
    assert len(sent) == 1 and sent[0].kind is EventKind.DOWNLOAD_FAILED
    assert "3 次" in sent[0].detail and "cookies" in sent[0].detail


def test_done_sends_success_and_removes_job(tmp_path):
    ad, _, sent = make(tmp_path)
    ad.on_ended(ended())
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    info = VideoInfo("youtube", "v1", "u", "t", "@a", "A", "20261110")
    result = DownloadResult(info, tmp_path, video=video, chat_queued=True)
    ad.finish(ad.next_due(), Outcome("done", result=result))
    assert ad.load() == []
    assert sent[0].kind is EventKind.DOWNLOADED
    assert "v.mp4" in sent[0].detail and "自动补抓" in sent[0].detail


def test_job_added_during_attempt_is_kept(tmp_path):
    ad, _, _ = make(tmp_path, keep={A.id: 2, B.id: 2})
    ad.on_ended(ended(A))
    job = ad.next_due()
    ad.on_ended(ended(B, "b1"))  # arrives while A downloads
    ad.finish(job, Outcome("wait", "x"))
    assert sorted(j.status.streamer.id for j in ad.load()) == sorted([A.id, B.id])


# ---- progress snapshot (what the archives page shows) ----

def test_snapshot_lists_queued_jobs(tmp_path):
    ad, clock, _ = make(tmp_path)
    assert ad.snapshot() == {"active": None, "queue": []}
    ad.on_ended(ended())
    job = ad.next_due()
    ad.finish(job, Outcome("wait", "stream is post_live"))
    (row,) = ad.snapshot()["queue"]
    assert row["name"] == "A" and row["title"] == "title v1"
    assert row["attempts"] == 1 and row["last_error"] == "stream is post_live"
    assert row["next_try"] == clock.t + 900


# ---- attempt ----

class FakeDL:
    def __init__(self, status="was_live", vod="https://youtu.be/v1"):
        self.status, self.vod = status, vod

    def find_vod(self, url, channel, started_at):
        return self.vod

    def info(self, url):
        return VideoInfo("youtube", "v1", url, "t", "@a", "A", "20261110", live_status=self.status)


@pytest.mark.parametrize("status,vod,kind", [
    ("post_live", "u", "wait"),
    ("was_live", None, "wait"),
])
def test_attempt_not_ready(tmp_path, monkeypatch, status, vod, kind):
    ad, _, _ = make(tmp_path)
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: FakeDL(status, vod))
    ad.on_ended(ended())
    assert ad.attempt(ad.next_due()).kind == kind


def test_attempt_downloads_and_prunes(tmp_path, monkeypatch):
    ad, _, _ = make(tmp_path)
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: FakeDL())
    out = ad.settings.output_dir / "A" / "20261110" / "t"

    def fake_download(url, settings, streamers, info=None, progress=None):
        out.mkdir(parents=True)
        return DownloadResult(info, out, video=out / "x.mp4")

    monkeypatch.setattr(auto_mod, "download", fake_download)
    ad.on_ended(ended())
    outcome = ad.attempt(ad.next_due())
    assert outcome.kind == "done" and outcome.result.output_dir == out


def test_snapshot_shows_progress_while_downloading(tmp_path, monkeypatch):
    ad, _, _ = make(tmp_path)
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: FakeDL())
    out = ad.settings.output_dir / "A" / "20261110" / "t"
    seen = {}

    def fake_download(url, settings, streamers, info=None, progress=None):
        out.mkdir(parents=True)
        # yt-dlp progress hook payload (fragmented download: estimate, no total)
        progress({"status": "downloading", "downloaded_bytes": 250, "total_bytes_estimate": 1000,
                  "speed": 50.0, "eta": 15})
        seen["active"] = ad.snapshot()["active"]
        return DownloadResult(info, out, video=out / "x.mp4")

    monkeypatch.setattr(auto_mod, "download", fake_download)
    ad.on_ended(ended())
    ad.attempt(ad.next_due())

    active = seen["active"]
    assert active["name"] == "A" and active["title"] == "title v1"
    assert active["percent"] == 25.0
    assert (active["downloaded"], active["total"], active["speed"], active["eta"]) == (250, 1000, 50.0, 15)
    assert ad.snapshot()["active"] is None  # cleared once the attempt is over


def test_attempt_video_error(tmp_path, monkeypatch):
    ad, _, _ = make(tmp_path)
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: FakeDL())

    def boom(*a, **k):
        raise DownloadError("Private video")

    monkeypatch.setattr(auto_mod, "download", boom)
    ad.on_ended(ended())
    assert ad.attempt(ad.next_due()) == Outcome("error", "Private video")


# ---- retention ----

def test_prune_keeps_newest(tmp_path):
    ad, _, _ = make(tmp_path)
    creator = ad.settings.output_dir / "A"
    folders = []
    for i, date in enumerate(["20261101", "20261102", "20261102", "20261105"]):
        f = creator / date / f"title{i}"
        f.mkdir(parents=True)
        (f / "v.mp4").write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))
        folders.append(f)
    queue = PendingChats(ad.settings.pending_file)
    info = VideoInfo("youtube", "old", "u", "t", "@a", "A", "20261101")
    queue.add(info, folders[0], "not ready")

    removed = ad.prune(folders[3], keep=2)

    assert removed == [folders[0], folders[1]]
    assert not (creator / "20261101").exists()  # empty date folder removed too
    assert folders[2].exists() and folders[3].exists()
    assert queue.load() == []  # its queued chat is forgotten


def test_prune_never_deletes_the_new_one(tmp_path):
    ad, _, _ = make(tmp_path)
    creator = ad.settings.output_dir / "A"
    new = creator / "20260101" / "old-date-but-new"
    newer = creator / "20261231" / "x"
    for f in (new, newer):
        f.mkdir(parents=True)
    assert ad.prune(new, keep=1) == [newer]
    assert new.exists()


def test_prune_refuses_outside_output_dir(tmp_path):
    ad, _, _ = make(tmp_path)
    stray = tmp_path / "elsewhere" / "a" / "b"
    stray.mkdir(parents=True)
    assert ad.prune(stray, keep=0) == []
    assert stray.exists()


# ---- wiring ----

def test_ended_reaches_auto_download_even_with_offline_messages_off(tmp_path):
    ad, _, _ = make(tmp_path)

    async def scenario():
        n = LiveNotifier([], notify_offline=False, offline_grace=0.01)
        n.on_ended.append(ad.on_ended)
        await n.on_change(off(), None)
        await n.on_change(live(), off())
        await n.on_change(off(), live())
        await asyncio.sleep(0.05)
        await n.aclose()

    asyncio.run(scenario())
    assert [j.status.stream_id for j in ad.load()] == ["v1"]


def test_load_auto_from_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [
            {"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True},
            {"name": "B", "platform": "twitch", "channel": "b", "auto_download": True, "keep": 2},
            {"name": "C", "platform": "youtube", "channel": "@c"},
        ]},
        "download": {"auto": {"keep": 6, "retry_minutes": 5}},
    }), encoding="utf-8")
    ad = load_auto(cfg)
    assert ad.keep == {"youtube:@a": 6, "twitch:b": 2}
    assert ad.retry_interval == 300

    cfg.write_text(json.dumps({"monitor": {"streamers": [{"platform": "youtube", "channel": "@c"}]}}), encoding="utf-8")
    assert load_auto(cfg) is None


def test_discord_download_payloads():
    d = DiscordNotifier("https://example.invalid")
    ok = d.build_payload(StreamEvent(EventKind.DOWNLOADED, live(), detail="影片：x.mp4"))
    bad = d.build_payload(StreamEvent(EventKind.DOWNLOAD_FAILED, live(), detail="boom"))
    assert "回放已下载" in ok["content"] and ok["embeds"][0]["description"] == "影片：x.mp4"
    assert "失败" in bad["content"] and bad["allowed_mentions"] == {"parse": []}
    asyncio.run(d.aclose())


def test_video_format_caps_height():
    s = DownloadSettings(max_height=1080)
    assert "[height<=1080]" in YoutubeDownloader(s).video_format()
    assert TwitchDownloader(s).video_format().startswith("bv*[height<=1080]+ba")
    assert "height" not in TwitchDownloader(DownloadSettings(max_height=None)).video_format()
    assert Path  # keep import used
