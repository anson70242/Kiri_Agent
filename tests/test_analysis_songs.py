"""Song entries in the highlight table of a singing live, through analyze() with fake models.

Every transcript line here is invented: no real lyrics, no real streamer's speech.
"""

from __future__ import annotations

import json
import logging

import pytest

from backend.analysis import AnalysisSettings, analyze, is_singing_live
from backend.analysis import main as analysis_cli
from backend.archive import Archive

from .helpers import sample_archive


def tr(start, text, dur=2.0):
    return {"start": start, "end": start + dur, "text": text}


# A 12-minute singing live: rows 00:00–05:00, 05:00–10:00, 10:00–15:00.
# Song one runs 00:03:00–00:07:30 (crosses a row boundary), song two 00:11:00–00:12:00.
TRANSCRIPT = [
    tr(10, "こんばんは、今日はうたう日です"),
    tr(120, "最初の曲はみどりのかさです"),
    tr(180, "あめあめ ふらふら みどりのかさ"),
    tr(300, "かさをひらいて あるいていこう"),
    tr(440, "みどりのかさ さようなら"),
    tr(460, "ありがとうございました"),
    tr(600, "次はしずかな曲です"),
    tr(660, "ほしのした ゆらゆら ねむる"),
    tr(718, "おやすみなさい"),
]
CHAT = [{"time": "00:01:00", "user": "@a", "msg": "待ってた"}]

# What the segment pass writes for each row, by the row's start.
SEGMENT_LOGS = {
    "00:00:00": [
        {"start": "00:00:10", "end": "00:01:50", "topic": "开场", "title": "【开场】", "detail": "打招呼"},
        {"start": "00:02:00", "end": "00:02:50", "topic": "报歌", "title": "【报歌名】", "detail": "说要唱第一首"},
        {"start": "00:03:05", "end": "00:05:00", "topic": "唱歌", "title": "【唱了一首歌】", "detail": "唱歌中"},
    ],
    "00:05:00": [
        {"start": "00:05:00", "end": "00:07:20", "topic": "唱歌", "title": "【继续唱】", "detail": "唱歌中"},
        {"start": "00:07:40", "end": "00:09:50", "topic": "杂谈", "title": "【唱完道谢】", "detail": "道谢"},
    ],
    "00:10:00": [
        {"start": "00:10:00", "end": "00:10:50", "topic": "杂谈", "title": "【报下一首】", "detail": "报歌"},
        {"start": "00:11:10", "end": "00:12:00", "topic": "唱歌", "title": "【安静的歌】", "detail": "唱歌中"},
    ],
}
SCORES = [{"start": s, "score": 5, "reason": "普通", "highlights": [], "top_pick": None} for s in SEGMENT_LOGS]


class FakeLLM:
    """The chat completion: segment calls get SEGMENT_LOGS, the last call gets the scores."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        user = messages[-1]["content"]
        if "【本段】" in user:
            start = user.split("【本段】")[1][:8]
            return json.dumps({"timeline": SEGMENT_LOGS[start]}, ensure_ascii=False)
        return json.dumps({"scores": SCORES}, ensure_ascii=False)

    @property
    def scoring_input(self) -> str:
        return self.calls[-1][-1]["content"]


class FakeSongs:
    """The song-naming model: answers each call with the next reply (a dict is sent as
    fenced JSON, a str as it is, an exception is raised); the last reply repeats."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            return "好的，结果如下：\n```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"
        return reply


def song(start, end, title, artist="", title_sure=True, artist_sure=True, lyric="", evidence="主播报了歌名"):
    return {"start": start, "end": end, "title": title, "artist": artist, "title_sure": title_sure,
            "artist_sure": artist_sure, "lyric": lyric, "evidence": evidence}


TWO_SONGS = {"songs": [
    song("00:03:00", "00:07:30", "Midori no Kasa", "Invented Artist"),
    song("00:11:00", "00:12:00", "Hoshi no Shita", "Somebody", artist_sure=False),
]}


def make_live(tmp_path, transcript=TRANSCRIPT):
    folder = sample_archive(tmp_path, "A", "20261115", "歌枠", transcript=transcript, chat=CHAT,
                            chinese_transcript=None, highlights=None)
    return folder / "[20261115][A] 歌枠.mp4"


def run(tmp_path, songs=None, title="【歌枠】みんなでうたう", transcript=TRANSCRIPT, settings=None):
    video = make_live(tmp_path, transcript)
    llm = FakeLLM()
    analyze(video, llm, streamer="A", title=title, settings=settings, name_songs=songs)
    return Archive.from_video(video).read_highlights()["rows"], llm


def titles(row):
    return [e["title"] for e in row["timeline"]]


def test_songs_are_entries_in_the_row_where_they_start(tmp_path):
    rows, _ = run(tmp_path, FakeSongs(TWO_SONGS))

    first, second, third = rows
    assert titles(first) == ["【开场】", "【报歌名】", "【Midori no Kasa】"]
    assert first["timeline"][-1] == {"start": "00:03:00", "end": "00:07:30", "topic": "唱歌",
                                     "title": "【Midori no Kasa】", "detail": "原唱：Invented Artist"}
    assert "唱歌" in first["topics"]
    # the segment pass's own words for the song are gone; the talk after it stays
    assert titles(second) == ["【唱完道谢】"]
    assert titles(third) == ["【报下一首】", "【Hoshi no Shita】"]
    # a sure name with an unsure artist: the name alone
    assert third["timeline"][-1]["detail"] == ""


@pytest.mark.parametrize("title", ["【歌枠】夜", "初めての歌回", "カラオケ配信", "弾き語りします", "karaoke night",
                                   "Karaoke!", "KARAOKE"])
def test_each_title_word_makes_one_song_naming_call(tmp_path, title):
    songs = FakeSongs(TWO_SONGS)
    rows, llm = run(tmp_path, songs, title=title)

    assert len(songs.prompts) == 1
    assert "【Midori no Kasa】" in titles(rows[0])
    assert len(llm.calls) == 4  # the segment and scoring passes as before


def test_other_lives_make_no_song_naming_call(tmp_path):
    songs = FakeSongs(TWO_SONGS)
    rows, _ = run(tmp_path, songs, title="ゲーム雑談")

    assert songs.prompts == []
    assert [titles(r) for r in rows] == [[e["title"] for e in SEGMENT_LOGS[r["start"]]] for r in rows]


def test_a_singing_live_without_song_naming_is_analyzed_as_today(tmp_path):
    rows, _ = run(tmp_path, None, title="【歌枠】みんなでうたう")

    assert [r["timeline"] for r in rows] == [SEGMENT_LOGS[r["start"]] for r in rows]
    assert "唱歌" in rows[0]["topics"]  # the segment pass's own topic, as today


def test_the_song_naming_prompt_carries_the_whole_transcript_and_no_chat(tmp_path):
    songs = FakeSongs(TWO_SONGS)
    run(tmp_path, songs, title="【歌枠】みんなでうたう")

    (prompt,) = songs.prompts
    assert "A" in prompt and "【歌枠】みんなでうたう" in prompt
    assert "00:00:10 こんばんは、今日はうたう日です" in prompt
    assert "00:05:00 かさをひらいて あるいていこう" in prompt
    assert "00:11:58 おやすみなさい" in prompt
    assert "待ってた" not in prompt  # no chat
    # the reply keys the reading relies on are asked for
    assert all(key in prompt for key in ('"title_sure"', '"artist_sure"', '"lyric"', '"start"', '"end"'))


def test_a_trial_run_names_only_the_songs_within_the_limit(tmp_path):
    songs = FakeSongs(TWO_SONGS)  # the second song starts at 00:11:00, beyond the limit
    rows, _ = run(tmp_path, songs, settings=AnalysisSettings(limit_seconds=600))

    (prompt,) = songs.prompts
    assert "00:07:40 ありがとうございました" in prompt
    assert "次はしずかな曲です" not in prompt and "ほしのした" not in prompt
    assert [r["start"] for r in rows] == ["00:00:00", "00:05:00"]
    assert "【Hoshi no Shita】" not in str(rows)


def test_a_song_nobody_could_name_is_unnamed_with_the_heard_lyric(tmp_path):
    rows, _ = run(tmp_path, FakeSongs({"songs": [
        song("00:03:00", "00:07:30", "Midori no Kasa", "Invented Artist", title_sure=False,
             lyric="あめあめ ふらふら みどりのかさ"),
        song("00:11:00", "00:12:00", "", "", title_sure=True, lyric="ほしのした ゆらゆら"),
    ]}))

    first, third = rows[0]["timeline"][-1], rows[2]["timeline"][-1]
    assert (first["title"], first["detail"]) == ("【未识别】", "歌词：あめあめ ふらふら みどりのかさ")
    assert (third["title"], third["detail"]) == ("【未识别】", "歌词：ほしのした ゆらゆら")
    assert first["topic"] == third["topic"] == "唱歌"


def test_songs_without_a_start_inside_the_live_are_ignored(tmp_path):
    rows, _ = run(tmp_path, FakeSongs({"songs": [
        song("3:00", "00:07:30", "Bad Time"),  # not HH:MM:SS
        song("00:65:00", "00:66:00", "Bad Minutes"),
        song("01:00:00", "01:04:00", "After The Live"),
        {"end": "00:05:00", "title": "No Start", "title_sure": True},
        "not a song",
        song("00:11:00", "00:12:00", "Hoshi no Shita", "Somebody"),
    ]}))

    names = [e["title"] for r in rows for e in r["timeline"] if e["topic"] == "唱歌"]
    # the segment pass's entries stay where no valid song covers them
    assert names == ["【唱了一首歌】", "【继续唱】", "【Hoshi no Shita】"]


@pytest.mark.parametrize("end", [None, "", "later", "00:02:00", "00:03:00"])
def test_a_song_without_a_possible_end_is_kept_ending_at_its_start(tmp_path, end):
    item = song("00:03:00", end, "Midori no Kasa", "Invented Artist")
    if end is None:
        del item["end"]
    rows, _ = run(tmp_path, FakeSongs({"songs": [item]}))

    # covering no time, it removes none of the segment pass's entries
    assert titles(rows[0]) == ["【开场】", "【报歌名】", "【Midori no Kasa】", "【唱了一首歌】"]
    entry = rows[0]["timeline"][2]
    assert (entry["start"], entry["end"]) == ("00:03:00", "00:03:00")


def test_a_song_end_past_the_live_ends_with_the_live(tmp_path):
    rows, _ = run(tmp_path, FakeSongs({"songs": [song("00:11:00", "03:00:00", "Hoshi no Shita", "Somebody")]}))

    assert rows[2]["timeline"][-1]["end"] == "00:12:00"  # the last transcript line ends 00:12:00


def test_an_empty_song_list_changes_nothing(tmp_path):
    with_songs, _ = run(tmp_path / "a", FakeSongs({"songs": []}))
    without, _ = run(tmp_path / "b", None)

    assert with_songs == without


def test_a_reply_that_is_not_the_songs_json_is_retried(tmp_path):
    songs = FakeSongs("我搜索了一下，但是没找到", {"result": "no songs key"}, TWO_SONGS)
    rows, _ = run(tmp_path, songs)

    assert len(songs.prompts) == 3
    assert "【Midori no Kasa】" in titles(rows[0])


@pytest.mark.parametrize("reply", [RuntimeError("gateway said 502"), "not json at all"],
                         ids=["raises", "invalid every time"])
def test_failed_song_naming_leaves_the_table_as_today(tmp_path, caplog, reply):
    songs = FakeSongs(reply)
    with caplog.at_level(logging.WARNING, logger="backend.analysis"):
        rows, llm = run(tmp_path, songs)

    assert [r["timeline"] for r in rows] == [SEGMENT_LOGS[r["start"]] for r in rows]
    assert len(llm.calls) == 4  # the scoring pass still ran
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "song" in warning and ("gateway said 502" in warning or "not json at all" in warning)
    if isinstance(reply, str):
        assert len(songs.prompts) == AnalysisSettings().retries


def scoring_sections(llm) -> dict[str, str]:
    """The scoring input's description of each row, by the row's start."""
    return {part[3:11]: part for part in llm.scoring_input.split("\n\n") if part.startswith("## ")}


def test_the_scoring_pass_knows_which_song_was_sung_where(tmp_path):
    _, llm = run(tmp_path, FakeSongs(TWO_SONGS))

    sections = scoring_sections(llm)
    assert "【Midori no Kasa】" in sections["00:00:00"] and "【Hoshi no Shita】" in sections["00:10:00"]
    # a row in the middle of a song is described as part of it, not as dead air
    assert "【Midori no Kasa】" in sections["00:05:00"]
    assert "【Hoshi no Shita】" not in sections["00:05:00"]


def test_a_row_inside_a_long_song_keeps_an_empty_topic_log(tmp_path):
    rows, llm = run(tmp_path, FakeSongs({"songs": [song("00:03:00", "00:10:30", "Long Song", "Somebody")]}))

    assert (rows[1]["timeline"], rows[1]["topics"]) == ([], [])
    assert rows[0]["timeline"][-1]["end"] == "00:10:30"
    assert titles(rows[2]) == ["【安静的歌】"]  # 00:10:00 is inside the song, 00:11:10 after it
    assert "【Long Song】" in scoring_sections(llm)["00:05:00"]


def test_rows_keep_their_keys(tmp_path):
    rows, _ = run(tmp_path, FakeSongs(TWO_SONGS))

    assert all(list(r) == ["start", "end", "topics", "timeline", "highlights", "top_pick", "score", "reason",
                           "chat_count", "chat_ratio", "chat_level"] for r in rows)


def test_the_command_line_tool_names_songs_with_the_song_model(tmp_path, monkeypatch):
    video = make_live(tmp_path / "dl")
    llm = {"base_url": "http://gateway:8317/v1", "api_key_env": "AGY_API_KEY", "model": "chat-model", "proxy": True}
    monkeypatch.setenv("AGY_API_KEY", "sk-test")
    searched = []

    def fake_search(settings, model, prompt):
        searched.append((settings.proxy, model, prompt))
        return json.dumps(TWO_SONGS)

    monkeypatch.setattr("backend.llm.chat", lambda settings, messages, **kw: FakeLLM()(messages))
    monkeypatch.setattr("backend.llm.ask_with_search", fake_search)

    def cli(analysis, *args):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"llm": llm, "analysis": analysis}), encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["backend.analysis", str(video), "--streamer", "A",
                                         "--title", "【歌枠】みんなでうたう", "--config", str(cfg), *args])
        assert analysis_cli() == 0
        return Archive.from_video(video).read_highlights()["rows"]

    monkeypatch.setenv("LLM_PROXY", "http://llm-proxy:8318")
    rows = cli({"song_model": "search-model"})
    assert [(proxy, model) for proxy, model, _ in searched] == [("http://llm-proxy:8318", "search-model")]
    assert "【Hoshi no Shita】" in titles(rows[2])

    # ignoring the proxy, on a trial run of the first 10 minutes
    monkeypatch.delenv("LLM_PROXY")
    rows = cli({"song_model": "search-model"}, "--no-proxy", "--minutes", "10")
    proxy, _, prompt = searched[-1]
    assert proxy is None and "ほしのした" not in prompt
    assert "【Midori no Kasa】" in titles(rows[0]) and len(rows) == 2

    cli({"song_model": ""}, "--no-proxy")
    cli({}, "--no-proxy")
    assert len(searched) == 2  # no song model: no song naming


def test_is_singing_live_is_the_rule_callers_ask():
    assert is_singing_live("【歌回】ゆるっと") and is_singing_live("Karaoke time")
    assert not is_singing_live("マイクラ") and not is_singing_live("")
