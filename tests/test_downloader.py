"""Downloader logic without the network: naming, routing, metadata, Twitch chat paging."""

import json
from pathlib import Path

import httpx
import pytest

from backend.downloader import ChatNotReady, DownloadError, DownloadSettings, PendingChats, download, downloader_for
from backend.downloader.base import PlatformDownloader, VideoInfo, safe_name
from backend.downloader.twitch import TwitchDownloader, convert_comment, fetch_comments
from backend.downloader.youtube import YoutubeDownloader
from backend.monitor import Streamer

STREAMERS = [
    Streamer("東雲はる", "youtube", "@東雲はるch"),
    Streamer("柊優花", "twitch", "hiiragiyukaofficial"),
]


@pytest.mark.parametrize("url, cls", [
    ("https://www.youtube.com/watch?v=abc", YoutubeDownloader),
    ("https://youtu.be/abc", YoutubeDownloader),
    ("https://m.youtube.com/live/abc", YoutubeDownloader),
    ("https://www.twitch.tv/videos/123", TwitchDownloader),
    ("https://clips.twitch.tv/Slug", TwitchDownloader),
])
def test_routes_url_to_platform(url, cls):
    assert type(downloader_for(url, DownloadSettings())) is cls


def test_unsupported_url():
    with pytest.raises(DownloadError, match="unsupported"):
        downloader_for("https://twitcasting.tv/x/movie/1", DownloadSettings())


def test_safe_name():
    assert safe_name('a/b:c*?"<>|d') == "a_b_c______d"
    assert safe_name("  many   spaces. ") == "many spaces"
    assert safe_name("...") == "_"
    assert len(safe_name("x" * 500)) == 150


def test_youtube_info_matches_config_streamer():
    dl = YoutubeDownloader(DownloadSettings(), STREAMERS)
    raw = {
        "id": "abc", "title": "歌枠 Vol.2", "uploader_id": "@%E6%9D%B1%E9%9B%B2%E3%81%AF%E3%82%8Bch",
        "channel": "東雲はる / Shinonome Haru", "timestamp": 1_800_000_000, "extractor_key": "Youtube",
        "live_status": "was_live", "webpage_url": "https://www.youtube.com/watch?v=abc",
    }
    info = dl.parse_info(raw, "u")
    assert info.creator == "東雲はる"
    assert info.stem.startswith("[2027") and info.stem.endswith("][東雲はる] 歌枠 Vol.2")
    assert info.folder.parts[0] == "東雲はる"


def test_unknown_channel_falls_back_to_display_name():
    dl = TwitchDownloader(DownloadSettings(), STREAMERS)
    info = dl.parse_info({"id": "v42", "uploader_id": "lirik", "uploader": "LIRIK", "upload_date": "20261110",
                          "extractor_key": "TwitchVod"}, "u")
    assert (info.id, info.creator, info.date) == ("42", "LIRIK", "20261110")


def test_twitch_creator_match_is_case_insensitive():
    dl = TwitchDownloader(DownloadSettings(), STREAMERS)
    info = dl.parse_info({"id": "v1", "uploader_id": "HiiragiYukaOfficial", "uploader": "柊優花"}, "u")
    assert info.creator == "柊優花"


# ---- Twitch chat ----

def node(i: int, offset: int, text: str = "hi") -> dict:
    return {
        "id": f"c{i}", "createdAt": "2026-11-05T03:42:15Z", "contentOffsetSeconds": offset,
        "commenter": {"id": "9", "login": "bob", "displayName": "Bob"},
        "message": {"fragments": [{"text": text, "emote": None}], "userBadges": [], "userColor": "#fff"},
    }


def fake_gql(comments: list[dict], page: int = 3):
    """Mimics Twitch: from offset N, return up to `page` comments with offset >= N."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = json.loads(req.content)["query"]
        off = int(q.split("contentOffsetSeconds:")[1].split(")")[0])
        calls.append(off)
        rest = [c for c in comments if c["contentOffsetSeconds"] >= off]
        edges = [{"node": c} for c in rest[:page]]
        body = {"data": {"video": {"comments": {"edges": edges, "pageInfo": {"hasNextPage": len(rest) > page}}}}}
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_comments_pages_without_gaps_or_duplicates():
    src = [node(i, off) for i, off in enumerate([0, 1, 1, 2, 5, 5, 6, 9, 10, 10, 11])]
    client, _ = fake_gql(src)
    got = fetch_comments(client, "1")
    assert [c["_id"] for c in got] == [c["id"] for c in src]


def test_fetch_comments_steps_past_a_crowded_second():
    # 5 comments in second 3 with page size 3: must not loop forever.
    src = [node(i, 3) for i in range(5)] + [node(9, 4)]
    client, calls = fake_gql(src)
    got = fetch_comments(client, "1")
    assert "c9" in {c["_id"] for c in got}
    assert len(calls) < 10


def test_fetch_comments_missing_video():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"video": None}})))
    with pytest.raises(DownloadError, match="not found"):
        fetch_comments(client, "1")


def test_convert_comment_matches_twitchdownloader_layout():
    n = node(1, 7, "hello ")
    n["message"]["fragments"].append({"text": "Kappa", "emote": {"emoteID": "25"}})
    c = convert_comment(n)
    assert c["content_offset_seconds"] == 7
    assert c["commenter"]["display_name"] == "Bob"
    assert c["message"]["body"] == "hello Kappa"
    assert c["message"]["fragments"][1]["emoticon"] == {"emoticon_id": "25"}


def test_twitch_chat_refuses_clips(tmp_path):
    dl = TwitchDownloader(DownloadSettings())
    info = VideoInfo("twitch", "2625775811", "u", "t", "c", "C", "20261110", extractor="TwitchClips")
    with pytest.raises(DownloadError, match="clips"):
        dl.download_chat(info, tmp_path)


# ---- download() orchestration ----

class FakeDownloader(PlatformDownloader):
    platform = "fake"
    url_re = __import__("re").compile(r"^fake://")
    live_status = "was_live"
    fail: set[str] = set()

    def info(self, url):
        return VideoInfo("fake", "1", url, "Title", "c", "Chan", "20261110", live_status=self.live_status)

    def download_video(self, info, out_dir: Path) -> Path:
        if "video" in self.fail:
            raise DownloadError("video boom")
        p = out_dir / f"{info.stem}.mp4"
        p.write_bytes(b"x")
        return p

    def download_raw_chat(self, info, out_dir: Path) -> Path:
        if "chat" in self.fail:
            raise DownloadError("chat boom")
        if "chat_not_ready" in self.fail:
            raise ChatNotReady("not yet")
        p = out_dir / f"{info.stem}.chat.json"  # Twitch layout, so it gets cleaned
        p.write_text(json.dumps({"comments": [{"content_offset_seconds": 61, "commenter": {"display_name": "Bob"},
                                               "message": {"body": "hi"}}]}))
        return p


PlatformDownloader.registry.pop("fake")  # only registered while a test uses `fake`


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setitem(PlatformDownloader.registry, "fake", FakeDownloader)
    monkeypatch.setattr(FakeDownloader, "fail", set())
    monkeypatch.setattr(FakeDownloader, "live_status", "was_live")
    return FakeDownloader


def test_download_writes_into_creator_date_title(tmp_path, fake):
    r = download("fake://x", DownloadSettings(output_dir=tmp_path))
    assert r.ok
    assert r.output_dir == tmp_path / "Chan" / "20261110" / "Title"
    assert r.video.name == "[20261110][Chan] Title.mp4" and r.chat.exists()


def test_chat_failure_does_not_stop_video(tmp_path, fake):
    fake.fail = {"chat"}
    r = download("fake://x", DownloadSettings(output_dir=tmp_path))
    assert r.video.exists() and r.chat is None
    assert r.errors == {"chat": "chat boom"}


def test_total_failure_leaves_no_empty_folder(tmp_path, fake):
    fake.fail = {"video", "chat"}
    r = download("fake://x", DownloadSettings(output_dir=tmp_path))
    assert set(r.errors) == {"video", "chat"}
    assert not r.output_dir.exists()


def test_live_stream_is_refused(tmp_path, fake):
    fake.live_status = "is_live"
    with pytest.raises(DownloadError, match="is_live"):
        download("fake://x", DownloadSettings(output_dir=tmp_path))
    assert not any(tmp_path.iterdir())


def test_outtmpl_escapes_percent(tmp_path):
    from backend.downloader.base import outtmpl
    assert outtmpl(tmp_path, "100% 歌枠").name == "100%% 歌枠.%(ext)s"


# ---- chat retry queue ----

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def queued_settings(tmp_path):
    return DownloadSettings(output_dir=tmp_path / "dl", pending_file=tmp_path / "pending.json",
                            chat_retry_interval=1800, chat_retry_give_up=48 * 3600)


def test_not_ready_chat_is_queued_and_video_kept(tmp_path, fake):
    fake.fail = {"chat_not_ready"}
    st = queued_settings(tmp_path)
    r = download("fake://x", st)
    assert r.video.exists() and r.chat_queued
    [e] = PendingChats.from_settings(st).load()
    assert (e.info.url, e.output_dir, e.last_error) == ("fake://x", r.output_dir, "not yet")


def test_other_chat_errors_are_not_queued(tmp_path, fake):
    fake.fail = {"chat"}
    st = queued_settings(tmp_path)
    r = download("fake://x", st)
    assert not r.chat_queued and PendingChats.from_settings(st).load() == []


def test_post_live_refuses_video_but_queues_chat(tmp_path, fake):
    fake.live_status, fake.fail = "post_live", {"chat_not_ready"}
    st = queued_settings(tmp_path)
    r = download("fake://x", st)
    assert r.video is None and "processing" in r.errors["video"] and r.chat_queued
    assert not r.output_dir.exists()  # nothing downloaded yet; retry recreates it


def test_retry_waits_for_schedule_then_succeeds(tmp_path, fake):
    fake.fail = {"chat_not_ready"}
    st = queued_settings(tmp_path)
    clock = Clock()
    q = PendingChats(st.pending_file, 1800, 48 * 3600, clock)
    info = VideoInfo("fake", "1", "fake://x", "Title", "c", "Chan", "20261110")
    q.add(info, tmp_path / "out", "not yet")

    r = q.retry([], st)  # not due yet
    assert not (r.done or r.waiting) and len(q.load()) == 1

    clock.t += 1800
    r = q.retry([], st)  # due, still not ready -> rescheduled
    assert len(r.waiting) == 1 and q.load()[0].attempts == 1
    assert q.next_due() == clock.t + 1800

    fake.fail = set()
    clock.t += 1800
    r = q.retry([], st)
    [(e, path)] = r.done
    assert path == tmp_path / "out" / "[20261110][Chan] Title.chat_parsed.json" and path.exists()
    assert (path.parent / "[20261110][Chan] Title.chat.json").exists()  # raw kept
    assert q.load() == [] and q.next_due() is None


def test_retry_gives_up_after_deadline(tmp_path, fake):
    fake.fail = {"chat_not_ready"}
    st = queued_settings(tmp_path)
    clock = Clock()
    q = PendingChats(st.pending_file, 1800, 3600, clock)
    q.add(VideoInfo("fake", "1", "fake://x", "T", "c", "C", "20261110"), tmp_path, "x")
    clock.t += 3600
    r = q.retry([], st)
    assert len(r.given_up) == 1 and q.load() == []


def test_retry_force_ignores_schedule(tmp_path, fake):
    st = queued_settings(tmp_path)
    q = PendingChats(st.pending_file, 1800, 3600, Clock())
    q.add(VideoInfo("fake", "1", "fake://x", "T", "c", "C", "20261110"), tmp_path / "o", "x")
    assert len(q.retry([], st, force=True).done) == 1


def test_requeue_keeps_first_queue_time(tmp_path):
    clock = Clock()
    q = PendingChats(tmp_path / "p.json", 1800, 3600, clock)
    info = VideoInfo("fake", "1", "fake://x", "T", "c", "C", "20261110")
    q.add(info, tmp_path, "a")
    clock.t += 100
    q.add(info, tmp_path, "b")
    [e] = q.load()
    assert e.queued_at == 1_000_000.0 and e.last_error == "b"


def test_cookie_retry_prefers_cookies_file(tmp_path):
    f = tmp_path / "youtube_cookies.txt"
    s = DownloadSettings(cookies_file=f, cookies_from_browser="firefox")
    dl = YoutubeDownloader(s)
    assert dl.cookie_source()[1] == {"cookiesfrombrowser": ("firefox",)}  # file missing yet
    f.write_text("# Netscape HTTP Cookie File\n")
    assert dl.cookie_source()[1] == {"cookiefile": str(f)}
    assert YoutubeDownloader(DownloadSettings(cookies_file=tmp_path / "none")).cookie_source() is None


def test_clean_error_drops_prefix_and_colors():
    from backend.downloader.base import clean_error
    e = Exception("\n\x1b[0;31mERROR:\x1b[0m [youtube] x: members only. Use --cookies blah")
    assert clean_error(e) == "[youtube] x: members only."


# ---- chat cleaning ----

def yt_line(offset_ms, renderer: str, body: dict) -> str:
    return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(offset_ms), "actions": [
        {"addChatItemAction": {"item": {renderer: body}}}]}}, ensure_ascii=False)


def test_clean_youtube_chat(tmp_path):
    from backend.downloader.chat_clean import clean_chat
    raw = tmp_path / "[20261005][東雲はる] t.live_chat.json"
    raw.write_text("\n".join([
        yt_line(3_723_456, "liveChatTextMessageRenderer", {
            "authorName": {"simpleText": "@bob"},
            "message": {"runs": [{"text": "hi "},
                                 {"emoji": {"emojiId": "UC/x", "shortcuts": [":_chu:"], "isCustomEmoji": True}},
                                 {"emoji": {"emojiId": "😀", "shortcuts": [":grinning:"]}}]}}),
        yt_line(4_000_000, "liveChatPaidMessageRenderer", {
            "authorName": {"simpleText": "@spica"}, "purchaseAmountText": {"simpleText": "¥100"},
            "message": {"runs": [{"text": "おめでとう"}]}}),
        yt_line(5_000_000, "liveChatPaidStickerRenderer", {
            "authorName": {"simpleText": "@amy"}, "purchaseAmountText": {"simpleText": "$5.00"}}),
        yt_line(6_000_000, "liveChatMembershipItemRenderer", {"authorName": {"simpleText": "@new"}}),
        yt_line(7_000_000, "liveChatTextMessageRenderer", {"authorName": {"simpleText": "@e"}, "message": {"runs": []}}),
        "not json",
        "",
    ]), encoding="utf-8")
    out = clean_chat(raw)
    assert out.name == "[20261005][東雲はる] t.chat_parsed.json"
    assert json.loads(out.read_text(encoding="utf-8")) == [
        {"time": "01:02:03", "user": "@bob", "msg": "hi :_chu:😀"},
        {"time": "01:06:40", "user": "@spica", "msg": "おめでとう", "sc": "¥100"},
        {"time": "01:23:20", "user": "@amy", "msg": "[Super Sticker]", "sc": "$5.00"},
    ]
    # AutoKiri's layout: one message per line
    assert out.read_text(encoding="utf-8").count("\n") == 5


def test_clean_twitch_chat(tmp_path):
    from backend.downloader.chat_clean import clean_chat
    raw = tmp_path / "x.chat.json"
    raw.write_text(json.dumps({"comments": [
        convert_comment(node(1, 3725, "hello")),
        convert_comment(node(2, 3726, "   ")),
    ]}), encoding="utf-8")
    assert json.loads(clean_chat(raw).read_text(encoding="utf-8")) == [
        {"time": "01:02:05", "user": "Bob", "msg": "hello"}]


def test_clean_empty_chat_writes_empty_list(tmp_path):
    from backend.downloader.chat_clean import clean_chat
    raw = tmp_path / "x.chat.json"
    raw.write_text('{"comments": []}')
    assert json.loads(clean_chat(raw).read_text()) == []


def test_clean_rejects_unknown_file(tmp_path):
    from backend.downloader.chat_clean import parsed_path
    with pytest.raises(ValueError):
        parsed_path(tmp_path / "x.json")
