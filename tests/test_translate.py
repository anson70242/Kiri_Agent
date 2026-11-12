"""Chinese subtitles: grouped translation of the transcript, ids kept 1:1."""

import json
import re

import pytest

from backend.archive import Archive, Artifact
from backend.translate import TranslateError, TranslateSettings, translate


def make_transcript(tmp_path, n):
    video = tmp_path / "[20261111][A] t.mp4"
    video.write_bytes(b"x")
    segs = [{"start": float(i), "end": i + 0.9, "text": f"日本語{i}"} for i in range(n)]
    (tmp_path / "[20261111][A] t.transcript.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    return video


class FakeLLM:
    """Translates the lines it is asked for: '日本語7' -> '中文7'. Can drop ids or answer junk."""

    def __init__(self, drop=(), junk_first=0, wrap=False):
        self.drop = set(drop)  # ids it "forgets" (every time)
        self.junk_first = junk_first
        self.wrap = wrap  # answer {"data": [...]} instead of a bare array
        self.calls = []

    def __call__(self, messages):
        user = messages[-1]["content"]
        self.calls.append(user)
        if self.junk_first:
            self.junk_first -= 1
            return "I'm sorry"
        todo = json.loads(user.split("[翻译]")[1].split("[下文]")[0])
        out = [{"id": x["id"], "text": x["text"].replace("日本語", "中文")} for x in todo if x["id"] not in self.drop]
        return json.dumps({"data": out} if self.wrap else out, ensure_ascii=False)


def test_translates_every_line_in_groups(tmp_path):
    video = make_transcript(tmp_path, 200)
    llm = FakeLLM()
    out = translate(video, llm, TranslateSettings(group=80, context=5))

    assert out == tmp_path / "[20261111][A] t.zh.transcript.json"
    zh = json.loads(out.read_text(encoding="utf-8"))
    assert [s["text"] for s in zh] == [f"中文{i}" for i in range(200)]
    assert [(s["start"], s["end"]) for s in zh] == [(float(i), i + 0.9) for i in range(200)]  # same timing
    assert len(llm.calls) == 3  # 80 + 80 + 40


def test_context_lines_are_shown_but_not_asked(tmp_path):
    video = make_transcript(tmp_path, 200)
    llm = FakeLLM()
    translate(video, llm, TranslateSettings(group=80, context=5))

    second = llm.calls[1]
    before, todo, after = second.split("[上文]")[1].split("[翻译]")[0], second.split("[翻译]")[1].split("[下文]")[0], second.split("[下文]")[1]
    assert re.findall(r"日本語\d+", before) == [f"日本語{i}" for i in range(75, 80)]
    assert [x["id"] for x in json.loads(todo)] == list(range(80, 160))
    assert re.findall(r"日本語\d+", after) == [f"日本語{i}" for i in range(160, 165)]


def test_missing_ids_are_retried_then_kept_in_japanese(tmp_path):
    video = make_transcript(tmp_path, 10)
    llm = FakeLLM(drop={3})
    out = translate(video, llm, TranslateSettings(group=80, retries=2))
    zh = json.loads(out.read_text(encoding="utf-8"))
    assert zh[3]["text"] == "日本語3" and zh[4]["text"] == "中文4"
    assert len(llm.calls) == 2  # first try + one retry for the missing id


def test_wrapped_or_junk_replies(tmp_path):
    video = make_transcript(tmp_path, 10)
    out = translate(video, FakeLLM(wrap=True, junk_first=1), TranslateSettings(retries=3))
    assert json.loads(out.read_text(encoding="utf-8"))[9]["text"] == "中文9"


def test_limit_translates_only_the_start(tmp_path):
    video = make_transcript(tmp_path, 200)  # line i starts at i seconds
    llm = FakeLLM()
    out = translate(video, llm, TranslateSettings(group=80, limit_seconds=100))

    zh = json.loads(out.read_text(encoding="utf-8"))
    assert len(zh) == 200  # every line still there, timing unchanged
    assert zh[99]["text"] == "中文99" and zh[100]["text"] == "日本語100"  # the rest stays Japanese
    assert len(llm.calls) == 2  # 80 + 20


def test_nothing_usable_fails(tmp_path):
    video = make_transcript(tmp_path, 10)
    with pytest.raises(TranslateError):
        translate(video, FakeLLM(junk_first=99), TranslateSettings(retries=2))
    assert not Archive.from_video(video).has(Artifact.ZH_TRANSCRIPT)


def test_no_transcript_fails(tmp_path):
    video = tmp_path / "v.mp4"
    with pytest.raises(TranslateError, match="transcript"):
        translate(video, FakeLLM())
