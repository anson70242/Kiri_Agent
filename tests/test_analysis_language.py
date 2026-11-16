"""The highlight table is written in Chinese: a reply narrated in Japanese is asked again,
through analyze() with a fake model.

Every line here is invented: no real streamer's speech.
"""

from __future__ import annotations

import json
import logging

from backend.analysis import analyze
from backend.archive import Archive

from .helpers import sample_archive

# One 5-minute row.
TRANSCRIPT = [{"start": 10.0, "end": 12.0, "text": "こんばんは"}, {"start": 200.0, "end": 202.0, "text": "またね"}]

CHINESE_LOG = [
    {"start": "00:00:10", "end": "00:03:00", "topic": "开场", "title": "【开场打招呼】", "detail": "主播向观众打招呼，聊了今天的安排。"},
    {"start": "00:03:00", "end": "00:05:00", "topic": "闭幕", "title": "【道别】", "detail": "主播和观众说再见。"},
]
JAPANESE_LOG = [
    {"start": "00:00:10", "end": "00:03:00", "topic": "声枯れ雑談", "title": "【はじまりのあいさつ】",
     "detail": "リスナーにあいさつをして、きょうの予定について楽しそうに語り合う。"},
    {"start": "00:03:00", "end": "00:05:00", "topic": "おわり", "title": "【おわかれ】",
     "detail": "リスナーにまたねと言って、配信をしずかに終わらせる。"},
]
SCORES = [{"start": "00:00:00", "score": 5, "reason": "普通的闲聊", "highlights": [], "top_pick": None}]


class ScriptedLLM:
    """The chat completion: each segment call gets the next of `logs` and each scoring call
    the next of `scores` (the last one repeats)."""

    def __init__(self, logs, scores=(SCORES,)):
        self.logs, self.scores = list(logs), list(scores)
        self.segment_prompts: list[str] = []
        self.scoring_prompts: list[str] = []

    def __call__(self, messages):
        user = messages[-1]["content"]
        if "【本段】" in user:
            self.segment_prompts.append(user)
            return json.dumps({"timeline": self._next(self.logs)}, ensure_ascii=False)
        self.scoring_prompts.append(user)
        return json.dumps({"scores": self._next(self.scores)}, ensure_ascii=False)

    @staticmethod
    def _next(replies):
        return replies.pop(0) if len(replies) > 1 else replies[0]


def run(tmp_path, llm, streamer="A"):
    folder = sample_archive(tmp_path, "A", "20261115", "杂谈", transcript=TRANSCRIPT, chat=[],
                            chinese_transcript=None, highlights=None)
    video = folder / "[20261115][A] 杂谈.mp4"
    analyze(video, llm, streamer=streamer, title="杂谈")
    return Archive.from_video(video).read_highlights()["rows"]


def test_a_topic_log_narrated_in_japanese_is_asked_again_in_chinese(tmp_path):
    llm = ScriptedLLM([JAPANESE_LOG, CHINESE_LOG])

    (row,) = run(tmp_path, llm)

    assert row["timeline"] == CHINESE_LOG
    assert row["topics"] == ["开场", "闭幕"]
    first, second = llm.segment_prompts
    assert "简体中文重写" not in first and "简体中文重写" in second  # the second try says what was wrong
    assert "こんばんは" in second  # and still carries the material


def test_japanese_names_originals_and_quotes_inside_chinese_are_not_asked_again(tmp_path):
    allowed = [
        {"start": "00:00:10", "end": "00:02:00", "topic": "开场", "title": "【ゆめの歌】",  # a song's name
         "detail": "ほしのゆめ向观众打招呼，高声说晚上好（こんばんはー），然后唱了《ゆめのうた》。"},
        {"start": "00:02:00", "end": "00:04:00", "topic": "游戏杂谈", "title": "【削土豆】",
         "detail": "主播发现拿到的居然是さつまいも（红薯），吐槽「これもじゃがいもなの」，弹幕爆笑。"},
        {"start": "00:04:00", "end": "00:05:00", "topic": "闭幕", "title": "【道别】", "detail": "ほしのゆめ说再见。"},
    ]
    llm = ScriptedLLM([allowed, CHINESE_LOG])

    (row,) = run(tmp_path, llm, streamer="ほしのゆめ")

    assert row["timeline"] == allowed
    assert len(llm.segment_prompts) == 1


def test_a_topic_log_that_stays_japanese_is_used_as_it_is(tmp_path, caplog):
    llm = ScriptedLLM([JAPANESE_LOG])

    with caplog.at_level(logging.WARNING, logger="backend.analysis"):
        (row,) = run(tmp_path, llm)  # no AnalysisError: a table in Japanese beats no table

    assert row["timeline"] == JAPANESE_LOG
    assert len(llm.segment_prompts) == 3  # the usual number of tries
    assert any("Japanese" in r.getMessage() and "00:00:00" in r.getMessage() for r in caplog.records)


# ---- the scoring pass ----

def scores(title, reason="开场很有趣，适合剪辑。", highlight="主播突然大笑"):
    return [{"start": "00:00:00", "score": 8, "reason": "开场整活，全场最佳。",
             "highlights": [{"time": "00:00:10", "type": "笑点", "title": highlight}],
             "top_pick": {"start": "00:00:10", "end": "00:01:00", "title": title, "reason": reason}}]


def test_a_best_clip_title_in_japanese_is_asked_again_in_chinese(tmp_path):
    llm = ScriptedLLM([CHINESE_LOG], [scores("はじめてのあいさつで大わらいする件"), scores("开场问候直接笑场")])

    (row,) = run(tmp_path, llm)

    assert row["top_pick"]["title"] == "开场问候直接笑场"
    first, second = llm.scoring_prompts
    assert "简体中文重写" not in first and "简体中文重写" in second
    assert "【开场打招呼】" in second  # still the whole digest


def test_a_highlight_or_a_reason_in_japanese_is_asked_again_too(tmp_path):
    for i, japanese in enumerate([scores("开场问候直接笑场", highlight="とつぜん大きなこえでわらいだす"),
                                  scores("开场问候直接笑场", reason="あいさつのところがとてもおもしろいから")]):
        llm = ScriptedLLM([CHINESE_LOG], [japanese, scores("开场问候直接笑场")])
        run(tmp_path / str(i), llm)
        assert len(llm.scoring_prompts) == 2


def test_chinese_scores_with_japanese_originals_are_not_asked_again(tmp_path):
    fine = scores("ほしのゆめ开场笑场：「もうむり」名场面", reason="笑到说不出话（わらいすぎ），反差很大。",
                  highlight="高喊晚上好（こんばんはー）")
    llm = ScriptedLLM([CHINESE_LOG], [fine, scores("不该用到")])

    (row,) = run(tmp_path, llm, streamer="ほしのゆめ")

    assert row["top_pick"]["title"] == "ほしのゆめ开场笑场：「もうむり」名场面"
    assert len(llm.scoring_prompts) == 1


def test_scores_that_stay_japanese_are_used_as_they_are(tmp_path, caplog):
    llm = ScriptedLLM([CHINESE_LOG], [scores("はじめてのあいさつで大わらいする件")])

    with caplog.at_level(logging.WARNING, logger="backend.analysis"):
        (row,) = run(tmp_path, llm)

    assert row["score"] == 8 and row["top_pick"]["title"] == "はじめてのあいさつで大わらいする件"
    assert len(llm.scoring_prompts) == 3
    assert any("Japanese" in r.getMessage() and "scoring" in r.getMessage() for r in caplog.records)
