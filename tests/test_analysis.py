"""Stream analysis: 5-minute segments, chat stats, LLM passes, the 路灯表 file."""

import json

import pytest

from backend.analysis import AnalysisError, AnalysisSettings, analyze, build_segments, chat_levels
from backend.archive import Archive, Artifact


def tr(start, text, dur=2.0):
    return {"start": start, "end": start + dur, "text": text}


def msg(t, user="@a", text="草", sc=None):
    m = {"time": t, "user": user, "msg": text}
    if sc:
        m["sc"] = sc
    return m


def test_segments_split_transcript_with_context():
    transcript = [tr(10, "開幕"), tr(290, "前半の終わり"), tr(310, "後半の始まり"), tr(620, "最後")]
    segs = build_segments(transcript, [], seg_seconds=300, context_seconds=30)

    assert [(s.start, s.end) for s in segs] == [(0, 300), (300, 600), (600, 900)]
    first, second, third = segs
    assert [(l.text, l.context) for l in first.lines] == [("開幕", False), ("前半の終わり", False), ("後半の始まり", True)]
    assert [(l.text, l.context) for l in second.lines] == [("前半の終わり", True), ("後半の始まり", False), ("最後", True)]
    assert [l.text for l in third.lines] == ["最後"]


def test_segment_chat_stats():
    chat = [
        msg("00:00:00", text="待機"),  # waiting room (clamped to 0): ignored
        msg("00:01:00", user="Nightbot", text="!discord"),  # bot: ignored
        msg("00:01:00", text="草"), msg("00:01:30", text="草"), msg("00:02:00", text="かわいい"),
        msg("00:03:00", user="@rich", text="おめでとう", sc="¥500"),
        msg("00:06:00", text="草"),
    ]
    segs = build_segments([tr(10, "x"), tr(400, "y")], chat, seg_seconds=300)

    first, second = segs
    assert first.chat_count == 4 and second.chat_count == 1
    assert first.reactions[0] == ("草", 2)
    assert first.super_chats == [msg("00:03:00", user="@rich", text="おめでとう", sc="¥500")]
    # density relative to the whole stream: 5 messages over 10 minutes = 0.5/min average
    assert first.chat_ratio == 1.6 and second.chat_ratio == 0.4


def test_chat_levels_are_relative_ranks():
    # 10 segments: top 20% (2) 高, bottom 30% (3) 低, the rest 中 -- by rank, not by value.
    ratios = [0.1, 5.0, 0.3, 1.0, 0.2, 1.1, 0.9, 3.0, 1.2, 1.05]
    assert chat_levels(ratios) == ["低", "高", "低", "中", "低", "中", "中", "高", "中", "中"]
    assert chat_levels([]) == []
    assert chat_levels([1.0]) == ["中"]


def test_chat_sample_is_capped_and_in_order():
    chat = [msg(f"00:0{m}:{s:02d}", text=f"m{m}{s}") for m in range(1, 4) for s in range(0, 60, 1)]
    (seg,) = build_segments([tr(0, "x", 299)], chat, seg_seconds=300, sample_size=50)
    assert len(seg.sample) == 50
    assert [m["time"] for m in seg.sample] == sorted(m["time"] for m in seg.sample)


# ---- analyze(): the whole stream, with a fake LLM ----

class FakeLLM:
    """Answers segment calls with a topic log and the final call with scores."""

    def __init__(self, scores, bad_first=0):
        self.scores = scores
        self.bad_first = bad_first  # this many garbage replies first
        self.calls: list[list[dict]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        if self.bad_first:
            self.bad_first -= 1
            return "sorry, I can't"
        user = messages[-1]["content"]
        if "【本段】" in user:
            start = user.split("【本段】")[1][:8]
            return "```json\n" + json.dumps({"timeline": [
                {"start": start, "end": start, "topic": "游戏实况", "title": f"【{start}】", "detail": "细节"}]}) + "\n```"
        return json.dumps({"scores": self.scores})


def make_archive(tmp_path):
    video = tmp_path / "[20261111][鹿乃まほろ] 土豆.mp4"
    video.write_bytes(b"x")
    stem = video.name.removesuffix(".mp4")
    (tmp_path / f"{stem}.transcript.json").write_text(json.dumps(
        [tr(10, "6万個のじゃがいも"), tr(400, "まだチュートリアル?")], ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"{stem}.chat_parsed.json").write_text(json.dumps(
        [msg("00:01:00", text="草"), msg("00:01:10", text="草"), msg("00:06:40", text="かわいい", sc="¥500")],
        ensure_ascii=False), encoding="utf-8")
    return video


SCORES = [
    {"start": "00:00:00", "score": 8, "reason": "开场整活",
     "highlights": [{"time": "00:00:10", "type": "破防", "title": "6万个土豆"}],
     "top_pick": {"start": "00:00:10", "end": "00:01:00", "title": "开局吓哭", "reason": "反差"}},
    {"start": "00:05:00", "score": 5, "reason": "普通",
     "highlights": [{"time": "00:06:40", "type": "笑点", "title": "不该出现"}], "top_pick": None},
]


def test_analyze_writes_the_table(tmp_path):
    video = make_archive(tmp_path)
    llm = FakeLLM(SCORES)

    out = analyze(video, llm, streamer="鹿乃まほろ", title="土豆")

    assert out == tmp_path / "[20261111][鹿乃まほろ] 土豆.highlights.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    first, second = data["rows"]
    assert (first["start"], first["end"], second["start"]) == ("00:00:00", "00:05:00", "00:05:00")
    assert first["timeline"][0]["title"] == "【00:00:00】"
    assert (first["score"], first["reason"]) == (8, "开场整活")
    assert first["highlights"][0]["type"] == "破防" and first["top_pick"]["title"] == "开局吓哭"
    # below the threshold: no highlights even if the LLM wrote some
    assert second["score"] == 5 and second["highlights"] == [] and second["top_pick"] is None
    assert (first["chat_count"], second["chat_count"]) == (2, 1)
    assert {first["chat_level"], second["chat_level"]} <= {"高", "中", "低"}


def test_analyze_prompts_carry_the_material(tmp_path):
    video = make_archive(tmp_path)
    llm = FakeLLM(SCORES)
    analyze(video, llm, streamer="鹿乃まほろ", title="土豆")

    assert len(llm.calls) == 3  # 2 segments + 1 scoring pass
    seg1 = llm.calls[0][-1]["content"]
    assert "鹿乃まほろ" in seg1 and "土豆" in seg1
    assert "00:00:10 6万個のじゃがいも" in seg1
    assert "草 ×2" in seg1
    seg2 = llm.calls[1][-1]["content"]
    assert "¥500" in seg2 and "00:06:40 かわいい" in seg2
    scoring = llm.calls[2][-1]["content"]
    assert "【00:00:00】" in scoring and "【00:05:00】" in scoring


def test_limit_analyzes_only_the_first_minutes(tmp_path):
    video = make_archive(tmp_path)  # transcript reaches 00:06:42 -> 2 segments
    llm = FakeLLM(SCORES[:1])
    analyze(video, llm, streamer="A", title="t", settings=AnalysisSettings(limit_seconds=300))

    rows = Archive.from_video(video).read_highlights()["rows"]
    assert [r["start"] for r in rows] == ["00:00:00"]
    assert len(llm.calls) == 2  # 1 segment + scoring
    assert "まだチュートリアル" not in llm.calls[0][-1]["content"]  # 00:06:40: beyond the limit, not even context


def test_bad_json_is_retried(tmp_path):
    video = make_archive(tmp_path)
    analyze(video, FakeLLM(SCORES, bad_first=2), streamer="A", title="t")
    assert Archive.from_video(video).has(Artifact.HIGHLIGHTS)


def test_bad_json_every_time_fails(tmp_path):
    video = make_archive(tmp_path)
    with pytest.raises(AnalysisError, match="JSON"):
        analyze(video, FakeLLM(SCORES, bad_first=99), streamer="A", title="t")
    assert not Archive.from_video(video).has(Artifact.HIGHLIGHTS)


def test_no_transcript_fails(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(AnalysisError, match="transcript"):
        analyze(video, FakeLLM(SCORES), streamer="A", title="t")
