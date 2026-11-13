"""The chat of a TwitCasting replay, read from the movie's public comment history pages,
through the download entry point with the site and yt-dlp faked (tests/twitcasting_fakes.py).

The comment pages are hand-made: they keep only the structure verified on the real site
(2026-11-13): 20 comments a page, newest first, each a `tw-comment-history-item` with its
`data-comment-id`, text, commenter link and `<time datetime>`, and a pager linking the
last page. Names and texts are invented (the repository is public).
"""

from __future__ import annotations

import html
import json
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest

from backend.archive import Archive, Artifact, artifact_name, raw_chat_platform
from backend.downloader import download
from backend.downloader.twitcasting import TwitCastingDownloader

from .helpers import bare_archive
from .twitcasting_fakes import CHANNEL, MIKA, MOVIE, REPLAY, START, tc  # noqa: F401 (tc: fixture)

COMMENTS = f"https://twitcasting.tv/{CHANNEL}/moviecomment/{MOVIE}"
STEM = "[20261006][Mika] Night chat 013044"


def comment(cid: int, seconds: float, text: str = "hello", user: str = "viewer_a", name: str = "Viewer A") -> dict:
    """One comment, posted `seconds` after the live's start."""
    return {"id": str(cid), "user": user, "name": name, "text": text, "posted": START + timedelta(seconds=seconds)}


def comments_page(items: list[dict], index: int, last: int) -> str:
    """One comment history page: `items` newest first, and a pager up to page `last`."""
    rows = "".join(
        f'<div class="tw-comment-history-item"\n data-comment-id="{c["id"]}"\n>\n'
        '<div class="tw-comment-history-item__content">\n'
        f'<div class="tw-comment-history-item__content__text">\n {html.escape(c["text"])}   </div>\n</div>\n'
        '<div class="tw-comment-history-item__info"><div class="tw-comment-history-item__info__user">\n'
        f'<a href="/{c["user"]}" data-show-inspector="true"><img src="x.jpg" class="tw-comment-history-item__user__icon"></a>\n'
        '<div class="tw-comment-history-item__details"><div class="tw-comment-history-item__details__user">\n'
        f'<a class="tw-comment-history-item__details__user-link"\n href="/{c["user"]}"\n data-show-inspector="true"\n>\n'
        f'   {html.escape(c["name"])}   </a>\n</div></div></div>\n'
        f'<div><a href="/{CHANNEL}/comment/{MOVIE}-{c["id"]}">\n'
        '<time class="tw-comment-history-item__info__date" data-kind="relative" data-locale="en"\n'
        f' datetime="{format_datetime(c["posted"])}">\n 2026/10/06 01:31 </time></a></div>\n'
        "</div>\n</div>\n"
        for c in sorted(items, key=lambda c: c["posted"], reverse=True)
    )
    pager = "".join(f'<a href="/{CHANNEL}/moviecomment/{MOVIE}-{i}" aria-selected="{str(i == index).lower()}">{i + 1}</a>'
                    for i in sorted({0, index, last}))
    return ('<html lang="en"><body><main id="mainwrapper"><div class="tw-page">\n'
            f'<h2><ul><li><a href="/{CHANNEL}/movie/{MOVIE}"> Night chat / Mika\'s room </a></li></ul>'
            f" Comments ({len(items)})</h2>\n"
            f'<div class="tw-comment-history">\n{rows}</div>\n'
            f'<div class="tw-pager">{pager}</div>\n</div></main></body></html>')


def serve_comments(tc, comments: list[dict], per_page: int = 20) -> None:
    """Put a movie's comment history on the fake site: newest first, `per_page` a page."""
    newest_first = sorted(comments, key=lambda c: c["posted"], reverse=True)
    pages = [newest_first[i:i + per_page] for i in range(0, len(newest_first), per_page)] or [[]]
    for index, items in enumerate(pages):
        tc.site.pages[f"{COMMENTS}-{index}"] = comments_page(items, index, len(pages) - 1)
    tc.site.pages[COMMENTS] = tc.site.pages[f"{COMMENTS}-0"]


@pytest.fixture(autouse=True)
def waits(monkeypatch) -> list[float]:
    """The waits between comment page requests, recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr("backend.downloader.twitcasting.time.sleep", slept.append)
    return slept


def raw_chat(r) -> dict:
    return json.loads((r.output_dir / f"{STEM}.comments.json").read_text(encoding="utf-8"))


def chat(r) -> list[dict]:
    return json.loads(r.chat.read_text(encoding="utf-8"))


# ---- the raw chat ----

def test_every_comment_page_becomes_the_raw_chat_oldest_first(tc):
    comments = [comment(1000 + i, 1 + 10 * i, text=f"comment {i}", user=f"user_{i}", name=f"User {i}")
                for i in range(45)]
    serve_comments(tc, comments)

    r = download(REPLAY, tc.settings, [MIKA])

    assert r.ok, r.errors
    raw = raw_chat(r)
    assert raw["movie"] == {"id": MOVIE, "url": REPLAY, "title": "Night chat",
                            "started_at": START.isoformat(), "duration": 2383.0}
    assert len(raw["comments"]) == 45
    assert raw["comments"][0] == {"id": "1000", "user_id": "user_0", "name": "User 0", "text": "comment 0",
                                  "posted_at": (START + timedelta(seconds=1)).isoformat()}
    assert [c["id"] for c in raw["comments"]] == [str(1000 + i) for i in range(45)]


def test_comments_repeated_on_the_next_page_are_kept_once(tc):
    # A comment posted while the pages are read pushes the oldest of page 0 onto page 1.
    comments = [comment(2000 + i, 5 + i) for i in range(25)]
    serve_comments(tc, comments)
    page_1 = sorted(comments, key=lambda c: c["posted"], reverse=True)[19:]
    tc.site.pages[f"{COMMENTS}-1"] = comments_page(page_1, 1, 1)

    r = download(REPLAY, tc.settings, [MIKA])

    assert [c["id"] for c in raw_chat(r)["comments"]] == [str(2000 + i) for i in range(25)]


# ---- the chat artifact ----

def test_the_chat_times_match_the_video(tc):
    serve_comments(tc, [comment(1, 1, "first!", name="Viewer A"), comment(2, 1925, "ナイス", name="ビューアーB")])

    r = download(REPLAY, tc.settings, [MIKA])

    assert chat(r) == [{"time": "00:00:01", "user": "Viewer A", "msg": "first!"},
                       {"time": "00:32:05", "user": "ビューアーB", "msg": "ナイス"}]
    assert r.chat.name == f"{STEM}.chat_parsed.json"


def test_comments_outside_the_video_and_comments_without_text_are_left_out(tc):
    serve_comments(tc, [comment(1, -30, "waiting"), comment(2, 0, "start"), comment(3, 60, "  "),
                        comment(4, 2383, "last second"), comment(5, 2390, "otsu after the end")])

    r = download(REPLAY, tc.settings, [MIKA])

    assert [line["msg"] for line in chat(r)] == ["start", "last second"]
    assert len(raw_chat(r)["comments"]) == 5  # the raw chat keeps them all


def test_a_movie_without_comments_gives_an_empty_chat(tc):
    serve_comments(tc, [])

    r = download(REPLAY, tc.settings, [MIKA])

    assert r.ok, r.errors
    assert raw_chat(r)["comments"] == [] and chat(r) == []


# ---- reading the pages ----

def test_comment_pages_are_read_without_cookies_and_with_a_wait_between_them(tc, waits):
    serve_comments(tc, [comment(i, 1 + i) for i in range(50)])

    download(REPLAY, tc.settings, [MIKA])

    asked = [q for q in tc.site.requests if "/moviecomment/" in str(q.url)]
    assert [str(q.url) for q in asked] == [f"{COMMENTS}-0", f"{COMMENTS}-1", f"{COMMENTS}-2"]
    assert all("cookie" not in q.headers for q in asked)
    assert len(waits) == 2 and all(0 < w <= 5 for w in waits)


def flaky(tc, monkeypatch, failures: list) -> None:
    """The fake site, but the first requests for comment page 1 get `failures` (an HTTP
    status, or an exception raised instead of an answer)."""
    def answer(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{COMMENTS}-1" and failures:
            tc.site.requests.append(request)
            failure = failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, text="busy")
        return tc.site(request)
    monkeypatch.setattr(TwitCastingDownloader, "transport", httpx.MockTransport(answer))


@pytest.mark.parametrize("failures", [[503, 429], [httpx.ReadTimeout("timed out")]])
def test_a_transient_failure_of_a_comment_page_is_retried(tc, monkeypatch, failures):
    serve_comments(tc, [comment(i, 1 + i) for i in range(30)])
    flaky(tc, monkeypatch, failures)

    r = download(REPLAY, tc.settings, [MIKA])

    assert r.ok, r.errors
    assert len(raw_chat(r)["comments"]) == 30


def test_a_chat_failure_does_not_stop_the_video(tc):
    serve_comments(tc, [comment(i, 1 + i) for i in range(30)])
    del tc.site.pages[f"{COMMENTS}-1"]  # 404: not transient

    r = download(REPLAY, tc.settings, [MIKA])

    assert r.video.exists() and r.chat is None
    assert set(r.errors) == {"chat"} and "404" in r.errors["chat"]
    assert not (r.output_dir / f"{STEM}.comments.json").exists()


def test_a_comment_page_that_keeps_failing_is_a_chat_failure(tc, monkeypatch):
    serve_comments(tc, [comment(i, 1 + i) for i in range(30)])
    flaky(tc, monkeypatch, [503] * 10)

    r = download(REPLAY, tc.settings, [MIKA])

    assert r.video.exists() and "503" in r.errors["chat"]


def test_a_page_that_is_not_a_comment_history_is_a_chat_failure(tc):
    tc.site.pages[f"{COMMENTS}-0"] = "<html><body>front page</body></html>"

    r = download(REPLAY, tc.settings, [MIKA])

    assert r.video.exists() and "comment page" in r.errors["chat"]


def test_a_second_download_of_the_replay_does_not_read_the_comments_again(tc):
    serve_comments(tc, [comment(1, 1)])
    first = download(REPLAY, tc.settings, [MIKA])
    asked = len(tc.site.requests)

    second = download(REPLAY, tc.settings, [MIKA])

    assert second.ok and chat(second) == chat(first)
    assert not any("/moviecomment/" in str(q.url) for q in tc.site.requests[asked:])


# ---- the archive ----

def test_the_twitcasting_raw_chat_is_an_artifact_of_its_own(tmp_path):
    name = artifact_name("x", Artifact.RAW_CHAT, "twitcasting")
    assert raw_chat_platform(name) == "twitcasting"
    assert [a for a in Artifact if a.matches(name)] == [Artifact.RAW_CHAT]
    for other in ("youtube", "twitch"):
        assert raw_chat_platform(artifact_name("x", Artifact.RAW_CHAT, other)) == other
    folder = bare_archive(tmp_path, stem="x", video=b"v")
    (folder / name).write_text("{}", encoding="utf-8")
    assert Archive.from_folder(folder).file(Artifact.RAW_CHAT) == folder / name


def test_the_command_line_downloads_a_replay_with_its_chat(tc, monkeypatch, capsys):
    from backend.downloader import __main__ as cli

    serve_comments(tc, [comment(1, 1, "first!")])
    cfg = tc.tmp / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "Mika", "platform": "twitcasting", "channel": CHANNEL}]},
        "download": {"output_dir": str(tc.tmp / "dl"), "twitcasting_cookies_file": str(tc.cookies),
                     "chat_retry": {"state_file": str(tc.tmp / "pending.json")}},
    }), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["backend.downloader", "--config", str(cfg), REPLAY])

    assert cli.main() == 0
    assert f"{STEM}.chat_parsed.json" in capsys.readouterr().out
