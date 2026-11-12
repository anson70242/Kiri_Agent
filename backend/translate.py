"""Chinese subtitles: translate a stream's transcript, keeping every line's timing.

    from backend.translate import translate
    translate(video, complete)   # -> the Chinese transcript artifact next to the video

The transcript is sent in groups (default 80 lines) with a few lines of context on
each side. The LLM answers `{"lines": [{"id", "text"}]}`; ids map back 1:1 onto the
original lines, so timing never moves. Lines it leaves out are asked again; lines still
missing after the retries keep their Japanese text instead of failing the stream.
The prompt is AutoKiri's translator prompt without its streamer-specific parts.

    uv run -m backend.translate <video> [--no-proxy]
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.archive import Archive, ArtifactMissing, Artifact

log = logging.getLogger(__name__)

Complete = Callable[[list[dict[str, str]]], str]

SYSTEM = """### 角色设定与任务目标

你是专属的字幕翻译助手，专注于将日本虚拟主播（VTuber）的直播语音识别文本（Whisper 生成）翻译成流畅、通顺、接地气且带有日本轻小说 / VTuber 圈内风格的简体中文。

核心任务：你不仅仅是翻译，更是一名“字幕修复师”。你需要克服语音识别带来的断句破碎、语气词冗余和听译错误，输出适合观众阅读的字幕。

### 输入

- [上文] 和 [下文]：前后几行原文，只帮助理解，不要翻译。
- [翻译]：要翻译的行，JSON 数组，每项有 id 和 text。

输入特征：大量无意义的语气词、重复词；断句经常在句子中间切断（跨行破碎）；同音词错误。

### 必须严格遵守的执行规范

#### 1. 跨行语义重组（核心机制）
绝对禁止死板的逐行翻译！翻译第 N 行时，必须同时参考 N-1 和 N+1 行。
1. 重组：先在脑海中无视行号，把破碎的日文拼成一句完整的话。
2. 翻译：把这句完整的话翻成地道的中文。
3. 拆分：根据原字幕各行的长度，把译文自然地拆回各行。
例：
  Line A: 今日はですね、なんか、あの、お知らせみたいな、
  Line B: 大行な感じになってますが、あのー、ちょっとしばらくね、
  错误（逐行）：A「今天呢，感觉，那个，像通知一样的，」B「感觉很宏大，那个，稍微暂时呢，」
  正确（重组）：A「今天 虽说看着像是个公告回」B「搞得阵仗挺大的 接下来有一段时间」

#### 2. 强力去噪与省略
大胆删除口语废话，除非带有强烈感情色彩（如惊吓）：
- 无意义填充词：あの、その、えっと、なんか（口头禅时）、ま（发语词时）、ね（句尾无意义时）。
- 重复词：「もう、もうやだ」只译为“真是讨厌”。
- 自我修正：说错马上改口时，只翻改口后的内容。
一行全是废话时，text 可以是空字符串。

#### 3. 倒装句逻辑修正
日语口语常先说结论再补主语或原因，翻译时按中文逻辑调整语序。
例：それもあるのよ。素材が、実は。 → 其实那个的素材 也是有的哦

#### 4. 格式与标点规范
- 不要使用中文逗号（，）和句号（。），需要停顿处一律用空格代替。
- 每行译文保持单行，不要换行。
- 引用观众评论、读弹幕、转述他人对话时用「」。
- 输出的 id 与输入完全一致，每个输入 id 都要有一项。

#### 5. 翻译风格：自然的女性口语（轻小说风格）
- 拒绝机翻与直译，用中文母语者的说话习惯重组。例：“这是世间一般的看法。” → “大众普遍都这么认为吧。”
- 保持日本轻小说式口语（如“ちゃん”译为“酱”），接地气，可以用中文互联网语境的词（切片、发推、联动、牙白），但不要滥用烂梗。

#### 6. 听译纠错
按语境纠正语音识别的同音错误。例：待つまで → 末まで（月底），最強戦 → 埼京線。

### 输出
只输出 JSON：{"lines": [{"id": 1, "text": "译文"}, ...]}，不要任何解释。"""


class TranslateError(Exception):
    """No transcript, or the LLM never returned usable translations."""


@dataclass
class TranslateSettings:
    group: int = 80  # lines per LLM call
    context: int = 5  # lines of context shown before and after a group
    retries: int = 3  # attempts per group (missing lines are asked again)
    limit_seconds: int | None = None  # only translate the start of the stream (trial runs)


def zh_path(video: Path) -> Path:
    """Where `translate` writes the Chinese transcript. Only the tests still ask; ask the Archive module."""
    return Archive.from_video(video).save_path(Artifact.ZH_TRANSCRIPT)


def translate(video: Path, complete: Complete, settings: TranslateSettings | None = None) -> Path:
    """Translate the transcript of `video`'s archive and save its Chinese transcript."""
    s = settings or TranslateSettings()
    archive = Archive.from_video(video)
    try:
        lines: list[dict[str, Any]] = archive.read_transcript()
    except ArtifactMissing:
        raise TranslateError(f"no transcript for {video.name}") from None

    texts: dict[int, str] = {}
    count = len(lines) if not s.limit_seconds else sum(1 for x in lines if x["start"] < s.limit_seconds)
    for start in range(0, count, s.group):
        ids = list(range(start, min(start + s.group, count)))
        texts.update(translate_group(lines, ids, complete, s))
        log.info("translate %s: %d/%d lines", video.name, ids[-1] + 1, count)

    zh = [{"start": x["start"], "end": x["end"], "text": texts.get(i, x["text"])} for i, x in enumerate(lines)]
    missing = sum(1 for i in range(count) if i not in texts)
    if missing:
        log.warning("translate %s: %d lines kept in Japanese", video.name, missing)
    return archive.save(Artifact.ZH_TRANSCRIPT, zh)


def translate_group(lines: list[dict[str, Any]], ids: list[int], complete: Complete,
                    s: TranslateSettings) -> dict[int, str]:
    """Translations for `ids`. Missing ones are asked again; raises if no attempt
    produced anything at all (LLM down or answering nonsense)."""
    done: dict[int, str] = {}
    todo = ids
    for _ in range(s.retries):
        reply = complete([{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": group_prompt(lines, todo, s.context)}])
        got = parse_lines(reply)
        done.update({i: t for i, t in got.items() if i in todo})
        todo = [i for i in todo if i not in done]
        if not todo:
            break
    if not done:
        raise TranslateError(f"LLM returned no usable translation for lines {ids[0]}–{ids[-1]}")
    return done


def group_prompt(lines: list[dict[str, Any]], ids: list[int], context: int) -> str:
    first, last = ids[0], ids[-1]
    before = lines[max(0, first - context):first]
    after = lines[last + 1:last + 1 + context]
    todo = [{"id": i, "text": lines[i]["text"]} for i in ids]
    return "\n".join([
        "[上文]", *(x["text"] for x in before),
        "[翻译]", json.dumps(todo, ensure_ascii=False),
        "[下文]", *(x["text"] for x in after),
    ])


def parse_lines(reply: str) -> dict[int, str]:
    """{id: text} from a reply: {"lines": [...]}, any {"key": [...]}, or a bare array."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    out: dict[int, str] = {}
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and "id" in item and "text" in item:
            try:
                out[int(item["id"])] = " ".join(str(item["text"]).split())
            except (TypeError, ValueError):
                continue
    return out


def main() -> int:
    """uv run -m backend.translate <video> [--no-proxy]"""
    import argparse
    import functools
    import os
    import sys

    from dotenv import load_dotenv

    from backend.llm import LLMError, chat, load_llm

    p = argparse.ArgumentParser(description="Translate a stream's transcript into Chinese subtitles.")
    p.add_argument("video", type=Path)
    p.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json")
    p.add_argument("--no-proxy", action="store_true", help="ignore llm.proxy (running on the proxy machine itself)")
    p.add_argument("--minutes", type=int, help="only translate the first N minutes (the rest stays Japanese)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(args.config.parent / ".env")
    if args.no_proxy:
        os.environ.setdefault("LLM_PROXY", "unused")
    try:
        llm = load_llm(args.config)
    except LLMError as e:
        print(e, file=sys.stderr)
        return 1
    if args.no_proxy:
        llm.proxy = None
    t = json.loads(args.config.read_text(encoding="utf-8")).get("translate") or {}
    settings = TranslateSettings(group=int(t.get("group", 80)), context=int(t.get("context", 5)),
                                 limit_seconds=args.minutes * 60 if args.minutes else None)
    try:
        print(translate(args.video, functools.partial(chat, llm, response_format={"type": "json_object"}), settings))
    except TranslateError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
