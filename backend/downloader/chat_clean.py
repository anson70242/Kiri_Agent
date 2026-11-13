"""Turn a raw chat replay into a short, uniform list for reading and LLM prompts.

The result is the chat artifact (saved by backend.archive in AutoKiri's layout, one message per line):

    [
      {"time": "00:12:34", "user": "Bob", "msg": "こんゆかし〜"},
      {"time": "00:15:00", "user": "@spica_v", "msg": "おめでとう", "sc": "¥100"}
    ]

`time` is the offset from the start of the video. `sc` only appears on YouTube paid
messages (Super Chat / Super Sticker). Messages without any text are dropped.
Custom emotes are kept as their name (`:_chu:`, `Kappa`), standard emoji as the emoji.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.archive import Archive, Artifact, raw_chat_platform

log = logging.getLogger(__name__)

ChatLine = dict[str, str]


def fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---- YouTube (yt-dlp live_chat: one JSON object per line) ----

def parse_youtube(path: Path) -> list[ChatLine]:
    lines: list[ChatLine] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            replay = item.get("replayChatItemAction") or {}
            offset = replay.get("videoOffsetTimeMsec") or item.get("videoOffsetTimeMsec")
            for action in replay.get("actions") or []:
                renderers = ((action.get("addChatItemAction") or {}).get("item")) or {}
                for kind, r in renderers.items():
                    if line := youtube_line(kind, r, offset):
                        lines.append(line)
    return lines


def youtube_line(kind: str, r: dict[str, Any], offset: str | None) -> ChatLine | None:
    if kind == "liveChatTextMessageRenderer":
        msg, sc = youtube_runs(r.get("message")), None
    elif kind == "liveChatPaidMessageRenderer":
        msg, sc = youtube_runs(r.get("message")), simple_text(r.get("purchaseAmountText"))
    elif kind == "liveChatPaidStickerRenderer":
        label = (((r.get("sticker") or {}).get("accessibility") or {}).get("accessibilityData") or {}).get("label")
        msg, sc = f"[Super Sticker{': ' + label if label else ''}]", simple_text(r.get("purchaseAmountText"))
    else:  # memberships, engagement banners, polls...
        return None
    if not msg.strip() and not sc:
        return None
    if offset is not None:
        time = fmt_time(int(offset) / 1000)
    else:  # very old replays: only the "1:23:45" stamp shown next to the message
        time = stamp_to_time(simple_text(r.get("timestampText")) or "0:00")
    line = {"time": time, "user": simple_text(r.get("authorName")) or "Unknown", "msg": msg}
    if sc:
        line["sc"] = sc
    return line


def youtube_runs(message: dict[str, Any] | None) -> str:
    out = []
    for run in (message or {}).get("runs") or []:
        if "text" in run:
            out.append(run["text"])
        elif emoji := run.get("emoji"):
            shortcuts = emoji.get("shortcuts") or []
            if emoji.get("isCustomEmoji") and shortcuts:
                out.append(shortcuts[0])  # channel emote: ":_chu:"
            else:
                out.append(emoji.get("emojiId") or (shortcuts[0] if shortcuts else ""))
    return "".join(out)


def simple_text(obj: dict[str, Any] | None) -> str:
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    return "".join(r.get("text", "") for r in obj.get("runs") or [])


def stamp_to_time(stamp: str) -> str:
    try:
        parts = [int(p) for p in stamp.lstrip("-").split(":")]
    except ValueError:
        return "00:00:00"
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return fmt_time(seconds)


# ---- Twitch (TwitchDownloader layout, written by twitch.py) ----

def parse_twitch(path: Path) -> list[ChatLine]:
    data = json.loads(path.read_text(encoding="utf-8"))
    lines: list[ChatLine] = []
    for c in data.get("comments") or []:
        message = c.get("message") or {}
        msg = message.get("body") or ""
        if not msg.strip():
            continue
        commenter = c.get("commenter") or {}
        lines.append({
            "time": fmt_time(c.get("content_offset_seconds") or 0),
            "user": commenter.get("display_name") or commenter.get("name") or "Unknown",
            "msg": msg,
        })
    return lines


# ---- TwitCasting (comment history, written by twitcasting.py) ----

def parse_twitcasting(path: Path) -> list[ChatLine]:
    """`time` is the posted time less the live's start time. Comments posted before the
    start or after the end of the video (some arrive after the live ended) are dropped."""
    data = json.loads(path.read_text(encoding="utf-8"))
    movie = data.get("movie") or {}
    start = datetime.fromisoformat(movie["started_at"])
    duration = movie.get("duration")
    lines: list[ChatLine] = []
    for c in data.get("comments") or []:
        msg = c.get("text") or ""
        if not msg.strip():
            continue
        offset = (datetime.fromisoformat(c["posted_at"]) - start).total_seconds()
        if offset < 0 or (duration is not None and offset > duration):
            continue
        lines.append({"time": fmt_time(offset), "user": c.get("name") or c.get("user_id") or "Unknown", "msg": msg})
    return lines


# ---- files ----

# platform -> its raw chat's parser
PARSERS = {"youtube": parse_youtube, "twitch": parse_twitch, "twitcasting": parse_twitcasting}


def clean_chat(raw: Path) -> Path:
    """Parse a raw chat file (type from its suffix) and save the cleaned chat in its archive,
    named after the raw chat. Raises ValueError for a file that is not a raw chat."""
    platform = raw_chat_platform(raw.name)
    if platform is None:
        raise ValueError(f"not a raw chat file: {raw.name}")
    archive = Archive.from_file(raw)
    lines = PARSERS[platform](raw)
    out = archive.save(Artifact.CHAT, lines)
    log.info("chat cleaned: %d messages -> %s", len(lines), out.name)
    return out
