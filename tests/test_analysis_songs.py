"""Song entries in the highlight table of a singing live, through analyze() with fake models.

Every transcript line here is invented: no real lyrics, no real streamer's speech.
"""

from __future__ import annotations

import json
import logging

import pytest

from backend.analysis import AnalysisSettings, analyze, is_singing_live
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


def test_is_singing_live_is_the_rule_callers_ask():
    assert is_singing_live("【歌回】ゆるっと") and is_singing_live("Karaoke time")
    assert not is_singing_live("マイクラ") and not is_singing_live("")
