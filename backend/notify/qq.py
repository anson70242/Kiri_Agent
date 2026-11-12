"""QQ group notifications through the official QQ bot (q.qq.com).

Each QQ group the bot is in subscribes to streamers and event kinds (the 通知 page);
`QQNotifier.send` delivers an event to every enabled group that wants it, one group at a
time, retrying each a little and logging (never raising) a failure, so one bad group
doesn't make the whole event go out again. Groups are only known by the openid QQ gives
the bot when it is added or @-mentioned (see backend/notify/qq_gateway.py).

API facts this relies on (checked live, 2026-11-12):
- Token: POST https://bots.qq.com/app/getAppAccessToken {appId, clientSecret} ->
  {access_token, expires_in} (~2 h); calls send `Authorization: QQBot <token>`.
- POST https://api.sgroup.qq.com/v2/groups/{group_openid}/messages: msg_type 0 text,
  2 markdown ({"markdown": {"content": ...}}); proactive messages work without anyone
  @-ing the bot; plain URLs and markdown links are allowed.
- Tencent can't fetch i.ytimg.com covers, so a cover is downloaded here and uploaded to
  the bot's media library (素材管理: /v2/media-images prepare -> PUT -> complete), which
  gives a `qqbot-media://` reference for markdown `![x #Wpx #Hpx](ref)`. The library holds
  100 images (40035007 when full): then our oldest cover (file name "kiri-") is deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .base import EventKind, Notifier, StreamEvent
from .history import NotifyHistory

log = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API = "https://api.sgroup.qq.com"
LOCAL_TZ = timezone(timedelta(hours=8))  # the group lives in China / Hong Kong time
DEFAULT_KINDS = ["live", "offline", "downloaded", "uploaded"]
MEDIA_PREFIX = "kiri-"
LIBRARY_FULL = 40035007

FOOTER_LIVE = "切片小助手 开播通知"
FOOTER_DOWNLOAD = "切片小助手 自动下载"
FOOTER_HIGHLIGHTS = "切片小助手 自动路灯"
# Pipeline events: icon, text, footer (the same words as the Discord messages).
PIPELINE = {
    EventKind.DOWNLOADED: ("📥", "回放已下载", FOOTER_DOWNLOAD),
    EventKind.DOWNLOAD_FAILED: ("❗", "回放下载失败", FOOTER_DOWNLOAD),
    EventKind.TRANSCRIBE_FAILED: ("❗", "字幕转写失败", FOOTER_DOWNLOAD),
    EventKind.TRANSLATE_FAILED: ("❗", "中文字幕翻译失败", FOOTER_DOWNLOAD),
    EventKind.ANALYZED: ("🚦", "路灯表已生成，精华段落：", FOOTER_HIGHLIGHTS),
    EventKind.ANALYZE_FAILED: ("❗", "路灯表分析失败", FOOTER_HIGHLIGHTS),
    EventKind.UPLOADED: ("📤", "路灯表已上传腾讯文档", FOOTER_HIGHLIGHTS),
    EventKind.UPLOAD_FAILED: ("❗", "路灯表上传腾讯文档失败", FOOTER_HIGHLIGHTS),
}
KIND_LABELS = {"live": "开播", "offline": "下播", "downloaded": "录播已下载", "uploaded": "上传完成",
               "analyzed": "路灯表已生成", "download_failed": "下载失败", "transcribe_failed": "转写失败",
               "translate_failed": "翻译失败", "analyze_failed": "分析失败", "upload_failed": "上传失败"}


class QQError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class QQSettings:
    app_id: str
    secret: str = field(repr=False)


# ---- groups ----

class QQGroups:
    """The groups the bot knows (data/qq_groups.json), keyed by group openid.

    A group: {openid, name, enabled, streamers: [streamer id], kinds: [event kind],
    status: "active" | "removed" (bot kicked) | "muted" (proactive messages turned off),
    added_at}. New groups start switched off with no streamers.
    """

    def __init__(self, path: Path):
        self.path = path
        try:
            self._groups: dict[str, dict[str, Any]] = {
                g["openid"]: g for g in json.loads(path.read_text(encoding="utf-8"))}
        except FileNotFoundError:
            self._groups = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._groups.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[dict[str, Any]]:
        return [dict(g) for g in self._groups.values()]

    def get(self, openid: str) -> dict[str, Any] | None:
        g = self._groups.get(openid)
        return dict(g) if g else None

    def register(self, openid: str) -> tuple[dict[str, Any], bool]:
        """The group, and whether it is new. A known group that was removed comes back active."""
        if g := self._groups.get(openid):
            if g.get("status") == "removed":
                g["status"] = "active"
                self.save()
            return dict(g), False
        g = {"openid": openid, "name": "", "enabled": False, "streamers": [], "kinds": list(DEFAULT_KINDS),
             "status": "active", "added_at": datetime.now(timezone.utc).isoformat()}
        self._groups[openid] = g
        self.save()
        return dict(g), True

    def update(self, openid: str, **fields: Any) -> dict[str, Any]:
        g = self._groups[openid]
        g.update({k: v for k, v in fields.items() if v is not None})
        self.save()
        return dict(g)

    def remove(self, openid: str) -> None:
        del self._groups[openid]
        self.save()

    def targets(self, event: StreamEvent) -> list[dict[str, Any]]:
        sid, kind = event.status.streamer.id, str(event.kind)
        return [dict(g) for g in self._groups.values()
                if g.get("enabled") and g.get("status") == "active"
                and sid in g.get("streamers", []) and kind in g.get("kinds", [])]


# ---- messages ----

def escape(text: str) -> str:
    """Plain text inside QQ markdown."""
    return re.sub(r"([\\*_\[\]`~#>|])", r"\\\1", text or "")


def link(url: str) -> str:
    return f"[{url}]({url})"


def duration(start: datetime, end: datetime) -> str:
    minutes = max(0, int((end - start).total_seconds() // 60))
    h, m = divmod(minutes, 60)
    return f"{h} 小时 {m} 分" if h else f"{m} 分钟"


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a JPEG or PNG, read from its header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    i = 2
    while i + 9 < len(data) and data[:2] == b"\xff\xd8":
        if data[i] != 0xFF:
            return None
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def message(event: StreamEvent, cover: str | None = None) -> str:
    """The markdown text of one event. `cover`: a qqbot-media markdown image line."""
    s = event.status
    name, title = escape(s.streamer.name), escape(s.title or "")
    if event.kind is EventKind.LIVE:
        lines = [f"🔴 **{name}** 开播了！"]
        if cover:
            lines.append(cover)
        lines += [f"{name} · {s.streamer.platform}", title]
        info = []
        if s.viewers is not None:
            info.append(f"{s.viewers:,} 人观看")
        if s.started_at:
            info.append(f"开始于 {s.started_at.astimezone(LOCAL_TZ):%H:%M}")
        if info:
            lines.append("👀 " + " · ".join(info))
        if s.url:
            lines.append(link(s.url))
        lines.append(FOOTER_LIVE)
    elif event.kind is EventKind.OFFLINE:
        lines = [f"⚫ **{name}** 下播了", title]
        if event.ended_at and s.started_at:
            lines.append(f"⏱ 直播时长 {duration(s.started_at, event.ended_at)}")
        lines.append(FOOTER_LIVE)
    else:
        icon, verb, footer = PIPELINE[event.kind]
        lines = [f"{icon} **{name}** {verb}", title]
        for line in (event.detail or "").splitlines():
            lines.append(link(line.strip()) if re.fullmatch(r"https?://\S+", line.strip()) else escape(line))
        lines.append(footer)
    return "\n".join(line for line in lines if line)


# ---- API ----

class QQApi:
    def __init__(self, settings: QQSettings, client: httpx.AsyncClient):
        self.s, self.client = settings, client
        self._token, self._expires = "", 0.0

    async def token(self) -> str:
        if not self._token or time.time() > self._expires - 60:
            r = await self.client.post(TOKEN_URL, json={"appId": self.s.app_id, "clientSecret": self.s.secret})
            d = r.json()
            if not d.get("access_token"):
                raise QQError(f"QQ bot token refused: {d.get('message') or d.get('code') or r.status_code}")
            self._token, self._expires = d["access_token"], time.time() + float(d.get("expires_in") or 7200)
        return self._token

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"QQBot {await self.token()}", "X-Union-Appid": self.s.app_id}
        r = await self.client.request(method, API + path, headers=headers, json=body)
        try:
            d = r.json()
        except ValueError:
            d = {}
        if r.status_code >= 400 or (isinstance(d, dict) and d.get("code") and not d.get("id")):
            code = d.get("code") if isinstance(d, dict) else None
            raise QQError(f"QQ API {code or r.status_code}: {(d or {}).get('message') or r.text[:200]}", code)
        return d if isinstance(d, dict) else {}

    async def send_markdown(self, group: str, content: str, msg_id: str | None = None,
                            event_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"msg_type": 2, "markdown": {"content": content}, "msg_seq": int(time.time() * 1000) % 2**31}
        if msg_id:
            body["msg_id"] = msg_id
        if event_id:
            body["event_id"] = event_id
        if "qqbot-media://" in content:
            body["force_verify_image_resource"] = True
        return await self.call("POST", f"/v2/groups/{group}/messages", body)

    async def upload_image(self, data: bytes, content_type: str = "image/jpeg") -> str:
        """Put an image into the bot's media library; returns its qqbot-media:// reference."""
        md5 = hashlib.md5(data).hexdigest()
        ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(content_type, "jpg")
        body = {"file_name": f"{MEDIA_PREFIX}cover-{md5[:12]}.{ext}", "file_size": len(data),
                "content_type": content_type, "md5": md5, "sha1": hashlib.sha1(data).hexdigest()}
        try:
            prep = await self.call("POST", "/v2/media-images/prepare", body)
        except QQError as e:
            if e.code != LIBRARY_FULL or not await self._drop_oldest():
                raise
            prep = await self.call("POST", "/v2/media-images/prepare", body)
        put = await self.client.put(prep["presigned_url"], content=data, headers={"Content-Type": content_type})
        put.raise_for_status()
        etag = (put.headers.get("ETag") or "").strip('"').lower() or md5
        done = await self.call("POST", "/v2/media-images/complete", {"upload_id": prep["upload_id"], "md5": etag})
        return str(done["url"])

    async def _drop_oldest(self) -> bool:
        ours = [i for i in (await self.call("GET", "/v2/media-images")).get("images") or []
                if str(i.get("file_name", "")).startswith(MEDIA_PREFIX)]
        if not ours:
            return False
        oldest = min(ours, key=lambda i: int(i.get("created_at") or 0))
        await self.call("DELETE", f"/v2/media-images/{oldest['image_id']}/delete")
        return True


# ---- notifier ----

class QQNotifier(Notifier):
    name = "qq"
    logs_itself = True  # one history entry per group

    def __init__(self, settings: QQSettings, groups: QQGroups, client: httpx.AsyncClient | None = None,
                 history: NotifyHistory | None = None, gap: float = 1.0, tries: int = 2):
        self.settings, self.groups = settings, groups
        self.client = client or httpx.AsyncClient(timeout=30)
        self.api = QQApi(settings, self.client)
        self.history = history or NotifyHistory()
        self.gap, self.tries = gap, tries  # seconds between groups; tries per group

    async def send(self, event: StreamEvent) -> None:
        targets = self.groups.targets(event)
        if not targets:
            return
        cover = await self.cover(event.status.thumbnail) if event.kind is EventKind.LIVE else None
        text = message(event, cover)
        summary = text.splitlines()[0].replace("**", "")
        for i, g in enumerate(targets):
            if i and self.gap:
                await asyncio.sleep(self.gap)
            await self.deliver(g, text, summary)

    async def deliver(self, group: dict[str, Any], text: str, summary: str) -> bool:
        error = ""
        for attempt in range(self.tries):
            try:
                await self.api.send_markdown(group["openid"], text)
                self.history.add("qq", group["openid"], summary, True)
                return True
            except (QQError, httpx.HTTPError) as e:
                error = str(e) or type(e).__name__
                if isinstance(e, QQError) and e.code and 40000000 <= int(e.code) < 50000000:
                    break  # a rule (e.g. proactive messages off): retrying won't help
                if attempt + 1 < self.tries:
                    await asyncio.sleep(2)
        log.warning("qq: %s not delivered to %s: %s", summary, group.get("name") or group["openid"], error)
        self.history.add("qq", group["openid"], summary, False, error)
        return False

    async def cover(self, url: str | None) -> str | None:
        """The markdown image line for a cover, or None (then the message is text only)."""
        if not url:
            return None
        try:
            r = await self.client.get(url, follow_redirects=True, timeout=20)
            r.raise_for_status()
            data = r.content
            kind = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            ref = await self.api.upload_image(data, kind)
        except (QQError, httpx.HTTPError, KeyError) as e:
            log.warning("qq: cover not used (%s)", e)
            return None
        w, h = image_size(data) or (480, 270)
        scale = min(1.0, 480 / w) if w else 1.0
        return f"![封面 #{round(w * scale)}px #{round(h * scale)}px]({ref})"

    async def aclose(self) -> None:
        await self.client.aclose()
