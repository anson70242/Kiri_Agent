"""Stream analysis: turn a transcript + chat into the 路灯表 (highlight table).

    from backend.analysis import analyze
    analyze(video, complete, streamer="鹿乃まほろ", title="...")
    # -> the highlight table artifact next to the video

Passes (AutoKiri's clip_highlight, adapted):
1. Every 5-minute segment (with 30 s of context each side): transcript + a chat digest
   -> a topic log, about 3 entries of 1–3 minutes each.
   A singing live (`is_singing_live`), given a song-naming model (`name_songs`), then gets one
   call over the whole transcript -> its songs, merged into the topic logs as song entries
   (one per song, in the segment where it starts; the segment pass's own entries during a
   song are dropped). If that call fails, the topic logs stay as they are.
2. One call over the whole stream: every segment's topic log + chat density
   -> a score per segment (relative within the stream), and for segments scoring
   >= 7 the highlights and the best clip.
3. The table: one row per segment; chat level 高/中/低 from the segment's density rank.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.archive import Archive, ArtifactMissing, Artifact

log = logging.getLogger(__name__)

BOTS = {"nightbot", "streamelements", "moobot", "fossabot", "streamlabs"}
TYPES = "笑点、可爱、感人、高能/神操作、事故、唱歌、重大发表、闲聊"

# A chat completion: messages -> reply text (backend.llm.chat with settings bound).
Complete = Callable[[list[dict[str, str]]], str]
# Song naming: prompt text -> reply text, from a model that can search the web.
NameSongs = Callable[[str], str]

# A title containing one of these (letter case ignored) marks a singing live.
SINGING_LIVE_WORDS = ("歌枠", "歌回", "カラオケ", "弾き語り", "karaoke")
SONG_TOPIC = "唱歌"
UNNAMED_SONG = "未识别"

SEGMENT_SYSTEM = """# Role
你是专属的中文 B 站字幕组金牌运营助手，对日本虚拟主播（VTuber）有骨灰级了解。
你的任务：以“切片机”般的精度，无遗漏地记录直播内容，供剪辑组挑选切片。

# Input
每次给你直播中的一个 5 分钟段落：
- 日语字幕（语音识别结果，一行一句，行首是该句开始时间）。标注【上下文】的行只帮助你理解前后文，不要记录它们。
- 本段的弹幕摘要（数量、相对整场的密度、最多的反应、Super Chat、抽样），是观众反应的旁证。

# Language Protocol（最高优先级）
1. 所有输出必须是简体中文。
2. 禁止用日语叙述。只有特有名词、梗、名台词，才在中文后用括号附日语原文，例：“削土豆瞄准（エイム）”。

# Execution Protocol
1. 绝对锚定：只根据本段文本，时间取自字幕行首的时间，所见即所得，禁止推测或编造时间。
2. 全覆盖：timeline 覆盖【本段】的全部时间，严禁跳过。发呆、调设备、喝水、无意义的哼歌也要记录（如“调整设备中”“无意义杂谈”）。
   上一条的结束时间接近下一条的开始时间。通常写 3 条；内容确实很杂或很单调时可以 2–4 条。
3. 脑内纠错雷达：字幕有同音字/识别错误时按上下文纠正理解，例：健康→喧嘩（吵架），満足→Mac，内容がないよ→冷笑话；FPS 术语：芋→蹲坑。
4. CP / 社死雷达持续生效：主播之间的互动、口误、社死瞬间要写清楚。

# Output（只输出 JSON）
{"timeline": [{"start": "HH:MM:SS", "end": "HH:MM:SS", "topic": "话题分类",
  "title": "【短标题】", "detail": "用中文具体描述发生了什么，包括动作和情绪变化"}]}
- topic：2–8 个汉字的简体中文分类词，不是句子，不用日语。例：开场、教程关卡、FPS杂谈、读SC、宠物杂谈、游戏事故、闭幕。
- title：【】括起来的简短中文标题；detail 才写具体经过。"""

SCORE_SYSTEM = f"""你是 VTuber 切片组的选题负责人。下面是一整场直播按 5 分钟分段的内容记录（话题日志、弹幕密度）。
请在整场范围内横向比较，给每一段打 1–10 的切片价值分：
- 分数是整场内的相对值：最精彩的几段 8–10，普通闲聊 4–6，无内容（等待、调设备）1–3。
- 8 分以上的段落不超过总段数的两成。
- 以内容为主，弹幕密度为辅。
对 {{min_score}} 分以上的段落，再根据它的话题日志写出：
- highlights：真正好笑、破防或有剪辑价值的瞬间。time 取自话题日志里的时间（HH:MM:SS），不要编造；
  type 优先从 {TYPES} 里选，不贴切时自拟 2–6 字的准确标签（例：哀嚎、吐槽、嘴硬、社死），不要硬套；title 是中文描述。
- top_pick：本段最值得剪的一个片段（范围取自话题日志的时间）、20 字以内的标题党标题、为什么必火（涉及什么梗或情绪）。
所有输出用简体中文，梗和名台词在中文后用括号附日语原文。
只输出 JSON：{{{{"scores": [{{{{"start": "HH:MM:SS", "score": 7, "reason": "一句话理由",
  "highlights": [{{{{"time": "HH:MM:SS", "type": "...", "title": "..."}}}}],
  "top_pick": {{{{"start": "HH:MM:SS", "end": "HH:MM:SS", "title": "...", "reason": "..."}}}}}}}}]}}}}
每段一条，start 与输入的段落开始时间相同；低于 {{min_score}} 分的段落 highlights 为 []、top_pick 为 null。"""

SONG_PROMPT = """下面是一场日本 VTuber 歌回的日语字幕（语音识别结果，行首是开始时间；唱歌部分是识别出的歌词，有同音错字，也会漏识别）。
主播：{streamer}
直播标题：{title}

任务：找出主播唱的每一首歌。
- start：这首歌开始唱的时间；end：这首歌的最后一句歌词的时间，也就是主播再开口说话或报下一首歌之前的最后一句歌词。都取自字幕行首时间。
- title / artist：歌名和原唱，用官方写法的原文，不要照搬语音识别听到的写法。根据歌词、主播在唱歌前后说的话来判断；有搜索工具时用歌词去搜索确认。
- title_sure：歌名确认无误才为 true。认不出就 title 写 "未识别"、title_sure 为 false，绝不猜。
- artist_sure：原唱确认无误才为 true。和歌名分开判断：歌名确定、原唱不确定时，title_sure 为 true、artist_sure 为 false，绝不猜。
- lyric：这首歌里的一句歌词，照字幕原样抄一行，供人工查找。
- evidence：一句话说明依据。
没有唱歌就输出 {{"songs": []}}。
只输出 JSON：{{"songs": [{{"start": "HH:MM:SS", "end": "HH:MM:SS", "title": "...", "artist": "...", "title_sure": true, "artist_sure": true, "lyric": "...", "evidence": "..."}}]}}

[字幕]
{lines}"""


class AnalysisError(Exception):
    """Missing input, or the LLM kept answering something that isn't the JSON we asked for."""


@dataclass
class AnalysisSettings:
    seg_seconds: int = 300
    context_seconds: int = 30
    sample_size: int = 150
    min_score: int = 7  # segments from this score get highlights + the best clip
    retries: int = 3  # attempts per LLM call when the reply isn't valid JSON
    limit_seconds: int | None = None  # only the first part of the stream (trial runs)


@dataclass
class Line:
    start: float
    text: str
    context: bool  # outside the segment, only there to help understanding


@dataclass
class Segment:
    start: int
    end: int
    lines: list[Line] = field(default_factory=list)
    chat_count: int = 0
    chat_ratio: float = 0.0  # messages per minute / the stream's average
    reactions: list[tuple[str, int]] = field(default_factory=list)  # most common messages
    super_chats: list[dict[str, Any]] = field(default_factory=list)
    sample: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Song:
    """One song of a singing live, with the song entry the topic log shows for it."""

    start: int
    end: int
    entry: dict[str, str]


def is_singing_live(title: str) -> bool:
    """Whether the live's title marks it as a singing live (the songs then get named)."""
    folded = title.casefold()
    return any(word in folded for word in SINGING_LIVE_WORDS)


TIME = re.compile(r"(?:(\d{1,2}):([0-5]?\d)|(\d{1,3})):([0-5]\d)(?:[.,]\d+)?")


def parse_time(value: Any) -> int | None:
    """Whole seconds of a time a model wrote, read tolerantly: `HH:MM:SS`, `H:MM:SS`,
    `MM:SS`, with or without a fraction of a second. None when it is not a time."""
    m = TIME.fullmatch(value.strip()) if isinstance(value, str) else None
    if not m:
        return None
    hours, minutes = (int(m[1]), int(m[2])) if m[1] is not None else (0, int(m[3]))
    return hours * 3600 + minutes * 60 + int(m[4])


def seconds(t: str) -> int:
    """Whole seconds of a time we wrote ourselves (chat times); ValueError when it is not one."""
    if (s := parse_time(t)) is None:
        raise ValueError(f"not a time: {t!r}")
    return s


def hms(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def audience_chat(chat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat that reflects the stream: no waiting-room messages (clamped to 00:00:00
    by chat cleaning), no bots."""
    return [m for m in chat if m.get("time") != "00:00:00"
            and str(m.get("user", "")).lstrip("@").lower() not in BOTS]


def chat_levels(ratios: list[float], high: float = 0.2, low: float = 0.3) -> list[str]:
    """高 for the busiest `high` share of segments, 低 for the quietest `low` share, 中
    otherwise. Ranks within this stream, so a quiet stream still has its 高 moments."""
    n = len(ratios)
    order = sorted(range(n), key=lambda i: ratios[i], reverse=True)
    levels = ["中"] * n
    for i in order[: round(n * high)]:
        levels[i] = "高"
    for i in order[n - round(n * low):] if round(n * low) else []:
        levels[i] = "低"
    return levels


def build_segments(
    transcript: list[dict[str, Any]],
    chat: list[dict[str, Any]],
    seg_seconds: int = 300,
    context_seconds: int = 30,
    sample_size: int = 150,
    top_reactions: int = 10,
) -> list[Segment]:
    msgs = audience_chat(chat)
    duration = max([s["end"] for s in transcript] + [seconds(m["time"]) for m in msgs] + [1])
    segs = [Segment(i * seg_seconds, (i + 1) * seg_seconds) for i in range(math.ceil(duration / seg_seconds))]
    avg_per_min = len(msgs) / (len(segs) * seg_seconds / 60)

    for seg in segs:
        lo, hi = seg.start - context_seconds, seg.end + context_seconds
        seg.lines = [
            Line(s["start"], s["text"], context=not seg.start <= s["start"] < seg.end)
            for s in transcript
            if s["end"] > lo and s["start"] < hi
        ]
        in_seg = [m for m in msgs if seg.start <= seconds(m["time"]) < seg.end]
        seg.chat_count = len(in_seg)
        per_min = len(in_seg) / (seg_seconds / 60)
        seg.chat_ratio = round(per_min / avg_per_min, 2) if avg_per_min else 0.0
        seg.reactions = Counter(t for m in in_seg if (t := str(m.get("msg", "")).strip())).most_common(top_reactions)
        seg.super_chats = [m for m in in_seg if m.get("sc")]
        if len(in_seg) <= sample_size:
            seg.sample = in_seg
        else:  # evenly spread over the segment, in time order
            step = len(in_seg) / sample_size
            seg.sample = [in_seg[int(i * step)] for i in range(sample_size)]
    return segs


# ---- the passes ----

def analyze(
    video: Path,
    complete: Complete,
    streamer: str = "",
    title: str = "",
    settings: AnalysisSettings | None = None,
    name_songs: NameSongs | None = None,
) -> Path:
    """Analyze the archive `video` belongs to (needs its transcript; chat is optional) and
    save the highlight table in it, next to the video. Given `name_songs`, a singing live
    (see `is_singing_live`) gets a song entry for every song."""
    s = settings or AnalysisSettings()
    archive = Archive.from_video(video)
    try:
        transcript = archive.read_transcript()
    except ArtifactMissing:
        raise AnalysisError(f"no transcript for {video.name}") from None
    chat = archive.read(Artifact.CHAT) if archive.has(Artifact.CHAT) else []
    if s.limit_seconds:
        transcript = [x for x in transcript if x["start"] < s.limit_seconds]
        chat = [m for m in chat if seconds(m["time"]) < s.limit_seconds]

    segs = build_segments(transcript, chat, s.seg_seconds, s.context_seconds, s.sample_size)
    header = f"主播：{streamer}　直播标题：{title}"
    timelines = []
    for i, seg in enumerate(segs):
        reply = ask_json(complete, SEGMENT_SYSTEM, segment_prompt(seg, header), s.retries)
        timelines.append(reply.get("timeline") or [])
        log.info("analysis %s: segment %d/%d", video.name, i + 1, len(segs))

    songs: list[Song] = []
    if name_songs is not None and is_singing_live(title):
        try:  # never fails the analysis: without songs, the table is as it would be anyway
            songs = find_songs(name_songs, song_prompt(transcript, streamer, title), live_end(transcript), s.retries)
        except Exception as e:
            log.warning("analysis %s: song naming failed, no song entries: %s", video.name, e)
        timelines = [with_songs(tl, seg, songs) for seg, tl in zip(segs, timelines)]

    digest = "\n\n".join(segment_digest(seg, tl, songs) for seg, tl in zip(segs, timelines))
    scored = ask_json(complete, SCORE_SYSTEM.format(min_score=s.min_score), f"{header}\n\n{digest}", s.retries)
    by_start = {str(x.get("start")): x for x in scored.get("scores") or [] if isinstance(x, dict)}

    levels = chat_levels([seg.chat_ratio for seg in segs])
    rows = []
    for seg, tl, level in zip(segs, timelines, levels):
        sc = by_start.get(hms(seg.start), {})
        score = sc.get("score") if isinstance(sc.get("score"), int) else None
        keep = score is not None and score >= s.min_score
        rows.append({
            "start": hms(seg.start),
            "end": hms(seg.end),
            "topics": list(dict.fromkeys(e.get("topic", "") for e in tl if e.get("topic"))),
            "timeline": tl,
            "highlights": (sc.get("highlights") or []) if keep else [],
            "top_pick": sc.get("top_pick") if keep else None,
            "score": score,
            "reason": sc.get("reason", ""),
            "chat_count": seg.chat_count,
            "chat_ratio": seg.chat_ratio,
            "chat_level": level,
        })

    data = {"streamer": streamer, "title": title, "segment_seconds": s.seg_seconds, "rows": rows}
    out = archive.save(Artifact.HIGHLIGHTS, data)
    log.info("analysis done: %s (%d segments)", out.name, len(rows))
    return out


def segment_prompt(seg: Segment, header: str) -> str:
    return "\n".join([
        header,
        f"【本段】{hms(seg.start)}–{hms(seg.end)}（标注【上下文】的行是前后各 30 秒的上下文）",
        "",
        "[字幕]",
        *(f"{'【上下文】' if line.context else ''}{hms(line.start)} {line.text}" for line in seg.lines),
        "",
        f"[弹幕] 本段 {seg.chat_count} 条，是整场平均密度的 {seg.chat_ratio} 倍",
        "反应最多：" + ("、".join(f"{t} ×{n}" for t, n in seg.reactions) or "无"),
        "Super Chat：" + ("；".join(f"{m['sc']}「{m.get('msg', '')}」" for m in seg.super_chats) or "无"),
        "抽样：",
        *(f"{m['time']} {m.get('msg', '')}" for m in seg.sample),
    ])


def segment_digest(seg: Segment, timeline: list[dict[str, Any]], songs: Sequence[Song] = ()) -> str:
    """The segment as the scoring pass reads it: its topic log, after the songs that began
    before it and are still being sung (so a row inside a long song is not dead air)."""
    lines = [f"## {hms(seg.start)}–{hms(seg.end)}（弹幕密度为整场平均的 {seg.chat_ratio} 倍）"]
    lines += [f"- [{sg.entry['start']}–{sg.entry['end']}] [{SONG_TOPIC}] {sg.entry['title']}（接上段，本段仍在唱这首歌）"
              for sg in songs if sg.start < seg.start < sg.end]
    lines += [f"- [{e.get('start')}–{e.get('end')}] [{e.get('topic')}] {e.get('title')} {e.get('detail')}"
              for e in timeline]
    return "\n".join(lines)


# ---- song entries (singing lives) ----

def song_prompt(transcript: list[dict[str, Any]], streamer: str, title: str) -> str:
    lines = "\n".join(f"{hms(x['start'])} {x['text']}" for x in transcript)
    return SONG_PROMPT.format(streamer=streamer, title=title, lines=lines)


def live_end(transcript: list[dict[str, Any]]) -> int:
    return math.ceil(max([x["end"] for x in transcript] + [0]))


def find_songs(name_songs: NameSongs, prompt: str, end: int, retries: int = 3) -> list[Song]:
    """One song-naming call (retried while the reply is not the JSON asked for), read as
    songs within 00:00:00–`end`."""
    reply = ""
    for _ in range(retries):
        reply = name_songs(prompt)
        if (data := parse_json(reply)) is not None and isinstance(data.get("songs"), list):
            return sorted((sg for x in data["songs"] if (sg := read_song(x, end))), key=lambda sg: sg.start)
    raise AnalysisError(f"song naming did not return the songs JSON after {retries} tries: {reply[:200]!r}")


def read_song(item: Any, live_end: int) -> Song | None:
    """A song from the model's untrusted reply: None without a start inside the live; a
    missing or impossible end becomes the start; an unsure or empty name becomes 未识别."""
    if not isinstance(item, dict):
        return None
    start = parse_time(item.get("start"))
    if start is None or start > live_end:
        return None
    end = parse_time(item.get("end"))
    end = min(end, live_end) if end is not None and end > start else start
    name = str(item.get("title") or "").strip().strip("【】").strip()
    if name and name != UNNAMED_SONG and item.get("title_sure") is True:
        artist = str(item.get("artist") or "").strip()
        detail = f"原唱：{artist}" if artist and item.get("artist_sure") is True else ""
    else:
        name, lyric = UNNAMED_SONG, str(item.get("lyric") or "").strip()
        detail = f"歌词：{lyric}" if lyric else ""
    entry = {"start": hms(start), "end": hms(end), "topic": SONG_TOPIC, "title": f"【{name}】", "detail": detail}
    return Song(start, end, entry)


def with_songs(timeline: list[dict[str, Any]], seg: Segment, songs: list[Song]) -> list[dict[str, Any]]:
    """The segment's topic log with the song entries of the songs starting in it, in time
    order, and without the entries the segment pass wrote while a song was sung: those
    starting inside a song, and those with more than half their length inside one (the
    segment pass often starts its entry for a song at the announcement, before the first
    lyric line). An entry without a valid end or with no length is judged by its start."""
    def during_a_song(entry: dict[str, Any]) -> bool:
        start, end = parse_time(entry.get("start")), parse_time(entry.get("end"))
        if start is None:
            return False
        length = end - start if end is not None and end > start else 0
        return any(sg.start <= start < sg.end
                   or (length and 2 * (min(end, sg.end) - max(start, sg.start)) > length) for sg in songs)

    merged = [e for e in timeline if not during_a_song(e)]
    for sg in songs:
        if seg.start <= sg.start < seg.end:
            later = (i for i, e in enumerate(merged) if (t := parse_time(e.get("start"))) is not None and t > sg.start)
            merged.insert(next(later, len(merged)), dict(sg.entry))
    return merged


def ask_json(complete: Complete, system: str, user: str, retries: int = 3) -> dict[str, Any]:
    """One LLM call that must answer a JSON object; retried when it doesn't."""
    reply = ""
    for _ in range(retries):
        reply = complete([{"role": "system", "content": system}, {"role": "user", "content": user}])
        if (data := parse_json(reply)) is not None:
            return data
    raise AnalysisError(f"LLM did not return valid JSON after {retries} tries: {reply[:200]!r}")


def parse_json(text: str) -> dict[str, Any] | None:
    """The JSON object in a reply, tolerating ```json fences and text around it."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def main() -> int:
    """uv run -m backend.analysis <video> [--streamer NAME] [--title TITLE]

    With `analysis.song_model` in config.json, a singing live (by `--title`) gets song entries."""
    import argparse
    import functools
    import sys

    from dotenv import load_dotenv

    from backend.llm import LLMError, ask_with_search, chat, load_llm

    p = argparse.ArgumentParser(description="Build the 路灯表 for a downloaded, transcribed stream.")
    p.add_argument("video", type=Path)
    p.add_argument("--streamer", default="")
    p.add_argument("--title", default="")
    p.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json")
    p.add_argument("--no-proxy", action="store_true", help="ignore llm.proxy (running on the proxy machine itself)")
    p.add_argument("--minutes", type=int, help="only analyze the first N minutes (trial run)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(args.config.parent / ".env")
    try:
        llm = load_llm(args.config)
    except LLMError as e:
        if not args.no_proxy or "LLM_PROXY" not in str(e):
            print(e, file=sys.stderr)
            return 1
        os.environ["LLM_PROXY"] = "unused"
        llm = load_llm(args.config)
    if args.no_proxy:
        llm.proxy = None
    a = json.loads(args.config.read_text(encoding="utf-8")).get("analysis") or {}
    settings = AnalysisSettings(seg_seconds=int(a.get("segment_minutes", 5) * 60),
                                context_seconds=int(a.get("context_seconds", 30)), min_score=int(a.get("min_score", 7)),
                                limit_seconds=args.minutes * 60 if args.minutes else None)
    complete = functools.partial(chat, llm, response_format={"type": "json_object"})
    song_model = str(a.get("song_model") or "").strip()  # absent or empty: no song naming
    name_songs = functools.partial(ask_with_search, llm, song_model) if song_model else None
    try:
        print(analyze(args.video, complete, args.streamer, args.title, settings, name_songs))
    except AnalysisError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
