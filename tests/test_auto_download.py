"""Auto-download after a stream ends: queue, retries, failure messages, retention."""

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from backend.archive import Archive, Artifact
from backend.downloader import auto as auto_mod
from backend.downloader.auto import AutoDownloader, Outcome, load_auto
from backend.downloader.base import DownloadError, DownloadResult, DownloadSettings, VideoInfo
from backend.downloader.pending import PendingChats
from backend.downloader.twitch import TwitchDownloader
from backend.downloader.youtube import YoutubeDownloader
from backend.notify import EventKind, LiveNotifier, StreamEvent
from backend.notify.discord import DiscordNotifier

from .helpers import A, B, T0, bare_archive, later, live, off

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


# ---- transcription stage ----

def downloaded(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    info = VideoInfo("youtube", "v1", "u", "t", "@a", "A", "20261110")
    return Outcome("done", result=DownloadResult(info, tmp_path, video=video))


def test_download_done_moves_job_to_transcription(tmp_path):
    calls = []
    ad, _, sent = make(tmp_path, transcribe=calls.append)
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))

    assert [e.kind for e in sent] == [EventKind.DOWNLOADED]
    (job,) = ad.load()
    assert job.stage == "transcribe" and job.video == str(tmp_path / "v.mp4")
    assert ad.snapshot()["queue"][0]["stage"] == "transcribe"

    outcome = ad.attempt(ad.next_due())
    assert outcome.kind == "done" and calls == [tmp_path / "v.mp4"]
    ad.finish(job, outcome)
    assert ad.load() == []


def test_transcription_failures_give_up_with_message(tmp_path):
    def boom(video):
        raise RuntimeError("whisper request failed: 500 CUDA OOM")

    ad, clock, sent = make(tmp_path, transcribe=boom)
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))
    for _ in range(3):
        job = ad.next_due()
        ad.finish(job, ad.attempt(job))
        clock.t += 900

    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.TRANSCRIBE_FAILED, EventKind.READY]  # then: everything over
    assert "CUDA OOM" in sent[1].detail


def test_transcription_done_moves_job_to_analysis_and_reports_top_segments(tmp_path):
    def fake_analyze(video, streamer, title):
        out = video.with_name("v.highlights.json")
        rows = [{"start": f"00:{i * 5:02d}:00", "score": s, "topics": [f"话题{i}"],
                 "top_pick": {"title": f"切片{i}"} if s >= 7 else None}
                for i, s in enumerate([3, 9, 6, 7, 8, 5, 10])]
        out.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
        seen.append((video, streamer, title))
        return out

    seen = []
    ad, _, sent = make(tmp_path, transcribe=lambda v: None, analyze=fake_analyze)
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))  # transcribed

    (job,) = ad.load()
    assert job.stage == "analyze"
    ad.finish(job, ad.attempt(job))

    assert ad.load() == []
    assert seen == [(tmp_path / "v.mp4", "A", "title v1")]
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.ANALYZED, EventKind.READY]  # then: everything over
    detail = sent[1].detail
    # top 5 by score, best first
    assert [line.split()[0] for line in detail.splitlines()[:5]] == ["00:30:00", "00:05:00", "00:20:00", "00:15:00", "00:10:00"]
    assert "切片6" in detail and "★10" in detail


def test_after_transcription_analysis_and_translation_run_side_by_side(tmp_path):
    translated = []

    def fake_analyze(video, streamer, title):
        out = video.with_name("v.highlights.json")
        out.write_text(json.dumps({"rows": []}), encoding="utf-8")
        return out

    ad, _, sent = make(tmp_path, transcribe=lambda v: None, analyze=fake_analyze, translate=translated.append)
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))  # transcribed

    assert sorted(j.stage for j in ad.load()) == ["analyze", "translate"]
    ad.on_ended(ended())  # the same broadcast again: still not queued twice
    assert len(ad.load()) == 2

    tr_job = ad.next_due("translate")
    an_job = ad.next_due("analyze")
    ad.finish(tr_job, ad.attempt(tr_job))
    assert [j.stage for j in ad.load()] == ["analyze"]  # translation done, analysis untouched
    ad.finish(an_job, ad.attempt(an_job))

    assert ad.load() == [] and translated == [tmp_path / "v.mp4"]
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.ANALYZED, EventKind.READY]  # then: everything over


def test_translation_failures_give_up_with_message(tmp_path):
    def boom(video):
        raise RuntimeError("LLM returned no usable translation")

    ad, clock, sent = make(tmp_path, transcribe=lambda v: None, translate=boom)
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))  # transcribed -> translate only (no analysis configured)
    for _ in range(3):
        job = ad.next_due()
        ad.finish(job, ad.attempt(job))
        clock.t += 900

    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.TRANSLATE_FAILED, EventKind.READY]  # then: everything over


def test_analysis_failures_give_up_with_message(tmp_path):
    def boom(video, streamer, title):
        raise RuntimeError("LLM did not return valid JSON")

    ad, clock, sent = make(tmp_path, transcribe=lambda v: None, analyze=boom)
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))
    for _ in range(3):
        job = ad.next_due()
        ad.finish(job, ad.attempt(job))
        clock.t += 900

    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.ANALYZE_FAILED, EventKind.READY]  # then: everything over
    assert "valid JSON" in sent[1].detail


# ---- upload to Tencent Docs (per streamer, from the gear) ----

def analyzed_to(tmp_path, upload, uploads):
    """An AutoDownloader whose stream A is analyzed; returns it after the analysis finished."""
    def fake_analyze(video, streamer, title):
        out = video.with_name("v.highlights.json")
        out.write_text(json.dumps({"rows": []}), encoding="utf-8")
        return out

    ad, clock, sent = make(tmp_path, transcribe=lambda v: None, analyze=fake_analyze, upload=upload)
    ad.uploads = uploads
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))
    for _ in range(2):  # transcribe, analyze
        job = ad.next_due()
        ad.finish(job, ad.attempt(job))
    return ad, clock, sent


def test_analyzed_table_is_uploaded_for_streamers_with_upload_on(tmp_path):
    calls = []

    def fake_upload(table, file_id, streamer, title):
        calls.append((table, file_id, streamer, title))
        return "https://docs.qq.com/sheet/SHEET1?tab=t1"

    ad, _, sent = analyzed_to(tmp_path, fake_upload, {A.id: "SHEET1"})
    (job,) = ad.load()
    assert job.stage == "upload"
    ad.finish(job, ad.attempt(job))

    assert ad.load() == []
    assert calls == [(tmp_path / "v.highlights.json", "SHEET1", "A", "title v1")]
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.ANALYZED, EventKind.UPLOADED, EventKind.READY]  # then: everything over
    assert "https://docs.qq.com/sheet/SHEET1?tab=t1" in sent[2].detail


def test_no_upload_when_the_streamer_has_it_off(tmp_path):
    ad, _, sent = analyzed_to(tmp_path, lambda *a: "url", {B.id: "SHEET1"})  # only B uploads
    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.ANALYZED, EventKind.READY]  # then: everything over


def test_no_upload_without_credentials(tmp_path):
    # upload on in the gear, but no Tencent Docs token in .env yet: skipped quietly.
    ad, _, sent = analyzed_to(tmp_path, None, {A.id: "SHEET1"})
    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.ANALYZED, EventKind.READY]  # then: everything over


def test_upload_failures_give_up_with_message(tmp_path):
    def boom(table, file_id, streamer, title):
        raise RuntimeError("Tencent Docs API error 400006: token 过期")

    ad, clock, sent = analyzed_to(tmp_path, boom, {A.id: "SHEET1"})
    for _ in range(3):
        job = ad.next_due()
        ad.finish(job, ad.attempt(job))
        clock.t += 900

    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.ANALYZED, EventKind.UPLOAD_FAILED, EventKind.READY]  # then: everything over
    assert "400006" in sent[2].detail


def test_downloads_keep_going_while_a_transcription_runs(tmp_path, monkeypatch):
    release = threading.Event()
    downloaded_b = threading.Event()

    def slow_transcribe(video):
        assert release.wait(5), "transcription never released"

    def fake_download(url, settings, streamers, info=None, progress=None):
        downloaded_b.set()
        out = settings.output_dir / "B" / "20261110" / "b"
        out.mkdir(parents=True)
        return DownloadResult(info, out, video=out / "b.mp4")

    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: FakeDL())
    monkeypatch.setattr(auto_mod, "download", fake_download)
    ad, _, _ = make(tmp_path, keep={A.id: 2, B.id: 2}, transcribe=slow_transcribe)
    ad.clock = time.time
    ad.on_ended(ended(A))
    ad.finish(ad.next_due(), downloaded(tmp_path))  # A: waiting for transcription
    ad.on_ended(ended(B, "b1"))  # B: waiting for download

    async def scenario():
        worker = asyncio.create_task(ad.run())
        try:
            # B downloads while A's transcription is still blocked.
            assert await asyncio.to_thread(downloaded_b.wait, 3)
            assert "transcribe" in [a["stage"] for a in ad.snapshot()["active"]]
            release.set()
            for _ in range(100):
                await asyncio.sleep(0.02)
                if not [j for j in ad.load() if j.stage == "transcribe" and j.status.streamer == A]:
                    break
        finally:
            release.set()
            worker.cancel()

    asyncio.run(scenario())
    assert all(j.status.streamer != A for j in ad.load())  # A transcribed and done


# ---- progress snapshot (what the archives page shows) ----

def test_snapshot_lists_queued_jobs(tmp_path):
    ad, clock, _ = make(tmp_path)
    assert ad.snapshot() == {"active": [], "queue": []}
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

    def find_replay(self, url, channel, live_id, started_at):
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
        (seen["active"],) = ad.snapshot()["active"]
        return DownloadResult(info, out, video=out / "x.mp4")

    monkeypatch.setattr(auto_mod, "download", fake_download)
    ad.on_ended(ended())
    ad.attempt(ad.next_due())

    active = seen["active"]
    assert active["name"] == "A" and active["title"] == "title v1"
    assert active["percent"] == 25.0
    assert (active["downloaded"], active["total"], active["speed"], active["eta"]) == (250, 1000, 50.0, 15)
    assert ad.snapshot()["active"] == []  # cleared once the attempt is over


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
        f = bare_archive(ad.settings.output_dir, "A", date, f"title{i}", stem="v", video=b"x")
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

    assert ad.transcribe is None  # no "asr" section: download only

    # Nobody auto-downloads yet: still built, so the dashboard's gear can turn it on.
    other = tmp_path / "other"
    other.mkdir()
    cfg2 = other / "config.json"
    cfg2.write_text(json.dumps({"monitor": {"streamers": [{"platform": "youtube", "channel": "@c"}]}}), encoding="utf-8")
    assert load_auto(cfg2).keep == {}

    cfg2.write_text(json.dumps({"download": {"auto": {"enabled": False}}}), encoding="utf-8")
    assert load_auto(cfg2) is None


def test_load_auto_with_analysis(tmp_path, monkeypatch):
    streamers = {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True}]}
    llm = {"base_url": "http://127.0.0.1:8317/v1", "api_key_env": "AGY_API_KEY", "model": "m", "proxy": False}
    asr = {"url": "http://whisper:8000"}  # analysis needs a transcript
    cfg = tmp_path / "config.json"

    monkeypatch.setenv("AGY_API_KEY", "sk-test")
    cfg.write_text(json.dumps({"monitor": streamers, "asr": asr, "llm": llm, "analysis": {"min_score": 6}}), encoding="utf-8")
    assert load_auto(cfg).analyze is not None

    assert load_auto(cfg).translate is None  # no "translate" section: off

    cfg.write_text(json.dumps({"monitor": streamers, "asr": asr, "llm": llm, "translate": {"group": 60}}), encoding="utf-8")
    ad = load_auto(cfg)
    assert ad.translate is not None and ad.analyze is None  # no "analysis" section: off

    no_key = llm | {"api_key_env": "KIRI_TEST_NO_SUCH_KEY"}  # (the repo .env gets loaded, so not AGY_API_KEY)
    cfg.write_text(json.dumps({"monitor": streamers, "asr": asr, "llm": no_key, "analysis": {}}), encoding="utf-8")
    ad = load_auto(cfg)  # LLM not usable: analysis off, the rest still runs
    assert ad is not None and ad.analyze is None


def test_load_auto_with_asr(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True}]},
        "asr": {"url": "http://whisper:8000", "language": "ja"},
    }), encoding="utf-8")
    ad = load_auto(cfg)
    assert ad.transcribe is not None
    assert ad.transcribe.keywords["settings"].url == "http://whisper:8000"


def test_discord_download_payloads():
    d = DiscordNotifier("https://example.invalid")
    ok = d.build_payload(StreamEvent(EventKind.DOWNLOADED, live(), detail="影片：x.mp4"))
    bad = d.build_payload(StreamEvent(EventKind.DOWNLOAD_FAILED, live(), detail="boom"))
    assert "回放已下载" in ok["content"] and ok["embeds"][0]["description"] == "影片：x.mp4"
    assert "失败" in bad["content"] and bad["allowed_mentions"] == {"parse": []}
    asr = d.build_payload(StreamEvent(EventKind.TRANSCRIBE_FAILED, live(), detail="whisper down"))
    assert "字幕转写失败" in asr["content"] and asr["embeds"][0]["description"] == "whisper down"
    done = d.build_payload(StreamEvent(EventKind.ANALYZED, live(), detail="00:05:00 ★9 切片"))
    assert "路灯表" in done["content"] and done["embeds"][0]["description"] == "00:05:00 ★9 切片"
    bad = d.build_payload(StreamEvent(EventKind.ANALYZE_FAILED, live(), detail="LLM down"))
    assert "分析失败" in bad["content"]
    asyncio.run(d.aclose())


def test_video_format_caps_height():
    s = DownloadSettings(max_height=1080)
    assert "[height<=1080]" in YoutubeDownloader(s).video_format()
    assert TwitchDownloader(s).video_format().startswith("bv*[height<=1080]+ba")
    assert "height" not in TwitchDownloader(DownloadSettings(max_height=None)).video_format()
    assert Path  # keep import used


def test_load_auto_uploads_only_with_credentials(tmp_path, monkeypatch):
    names = {"token_env": "KIRI_T_TOKEN"}  # never in a real .env
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True,
                                   "upload": True, "upload_sheet": "SHEET1"}]},
        "upload": {"tencent": names},
        "asr": {"url": "http://whisper:8000"},  # uploading needs an analysis, which needs both
        "llm": {"base_url": "http://127.0.0.1:8317/v1", "api_key_env": "KIRI_T_LLM", "model": "m", "proxy": False},
        "analysis": {},
    }), encoding="utf-8")
    monkeypatch.setenv("KIRI_T_LLM", "sk-test")
    ad = load_auto(cfg)
    assert ad.upload is None  # no token: uploads are skipped
    assert ad.uploads == {"youtube:@a": "SHEET1"}

    for name in names.values():
        monkeypatch.setenv(name, "x")
    assert load_auto(cfg).upload is not None


def test_downloaded_message_links_to_the_archive_page(tmp_path):
    ad, _, sent = make(tmp_path)
    ad.site_url = "https://kiri.example/"  # SITE_URL in .env
    ad.on_ended(ended())
    folder = bare_archive(tmp_path / "dl", "A", "20261110", "歌枠 (Vol.2)!", stem="v", video=b"x")
    video = Archive.from_folder(folder).video
    info = VideoInfo("youtube", "v1", "u", "t", "@a", "A", "20261110")
    ad.finish(ad.next_due(), Outcome("done", result=DownloadResult(info, folder, video=video)))

    event, ready = sent  # downloaded, then (no other stage) ready
    # the same encoding as the page's encodeURIComponent(folder), so its detail view opens
    assert event.detail.splitlines()[-1] == \
        "https://kiri.example/archives#A%2F20261110%2F%E6%AD%8C%E6%9E%A0%20(Vol.2)!"


def test_no_site_url_no_link(tmp_path):
    ad, _, sent = make(tmp_path)
    ad.on_ended(ended())
    ad.finish(ad.next_due(), downloaded(tmp_path))
    assert "http" not in sent[0].detail



def run_pipeline(tmp_path, translate_ok=True, site_url=""):
    """A stream through download -> transcribe -> analyze + translate; returns the events sent."""
    folder = bare_archive(tmp_path / "dl", "A", "20261110", "歌枠", stem="v", video=b"x", chat=[])
    archive = Archive.from_folder(folder)
    video = archive.video

    def transcribe(v):
        (folder / "v.transcript.json").write_text("[]", encoding="utf-8")

    def analyze(v, streamer, title):
        out = folder / "v.highlights.json"
        out.write_text(json.dumps({"rows": []}), encoding="utf-8")
        return out

    def translate(v):
        if not translate_ok:
            raise RuntimeError("LLM down")
        (folder / "v.zh.transcript.json").write_text("[]", encoding="utf-8")

    ad, clock, sent = make(tmp_path, transcribe=transcribe, analyze=analyze, translate=translate)
    ad.site_url = site_url
    ad.on_ended(ended())
    info = VideoInfo("youtube", "v1", "u", "t", "@a", "A", "20261110")
    ad.finish(ad.next_due(), Outcome("done", result=DownloadResult(info, folder, video=video, chat=archive.file(Artifact.CHAT))))
    for _ in range(10):
        job = ad.next_due()
        if job is None:
            clock.t += 900
            job = ad.next_due()
            if job is None:
                break
        ad.finish(job, ad.attempt(job))
    return sent


def test_ready_once_every_stage_is_over_with_the_download_link(tmp_path):
    sent = run_pipeline(tmp_path, site_url="https://kiri.example")
    kinds = [e.kind for e in sent]
    assert kinds.count(EventKind.READY) == 1 and kinds[-1] is EventKind.READY
    detail = sent[-1].detail
    assert "影片 ✅" in detail and "日文字幕 ✅" in detail and "中文字幕 ✅" in detail and "路灯表 ✅" in detail
    assert "https://kiri.example/api/archives/zip?folder=A%2F20261110%2F%E6%AD%8C%E6%9E%A0" in detail.splitlines()
    assert "https://kiri.example/archives#A%2F20261110%2F%E6%AD%8C%E6%9E%A0" in detail.splitlines()


def test_ready_still_comes_when_a_stage_gave_up(tmp_path):
    sent = run_pipeline(tmp_path, translate_ok=False)
    kinds = [e.kind for e in sent]
    assert EventKind.TRANSLATE_FAILED in kinds and kinds[-1] is EventKind.READY
    assert "中文字幕 ❌" in sent[-1].detail and "路灯表 ✅" in sent[-1].detail
    assert "http" not in sent[-1].detail  # no SITE_URL: no links


def test_no_ready_when_the_download_failed(tmp_path):
    def broken(*a, **kw):
        raise DownloadError("video unavailable")

    ad, clock, sent = make(tmp_path)
    ad.on_ended(ended())
    for _ in range(3):
        job = ad.next_due()
        ad.finish(job, Outcome("error", "video unavailable"))
        clock.t += 900
    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
