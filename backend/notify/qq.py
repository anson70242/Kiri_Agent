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
import logging
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.monitor import Streamer, StreamStatus
from backend.monitor.roster import drive_folder_as_typed
from backend.state_file import StateFile, expect_list

from .base import EventKind, Notifier, StreamEvent
from .history import NotifyHistory

log = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API = "https://api.sgroup.qq.com"
LOCAL_TZ = timezone(timedelta(hours=8))  # the group lives in China / Hong Kong time
DEFAULT_KINDS = ["upcoming", "live", "offline", "ready", "uploaded"]  # editors start once everything is ready
MEDIA_PREFIX = "kiri-"
LIBRARY_FULL = 40035007
# Lives whose uploaded cover is kept for reuse. Well under the library's 100: our oldest
# covers are deleted when it fills, so an older reference may point at nothing.
COVERS_KEPT = 20

FOOTER_LIVE = "切片小助手 直播通知"
FOOTER_DOWNLOAD = "切片小助手 自动下载"
FOOTER_HIGHLIGHTS = "切片小助手 自动路灯"
# Pipeline events: icon, text, footer (the same words as the Discord messages).
PIPELINE = {
    EventKind.DOWNLOADED: ("📥", "回放已下载", FOOTER_DOWNLOAD),
    EventKind.REPLAY_UNPUBLISHED: ("⏳", "回放未公开", FOOTER_DOWNLOAD),
    EventKind.DOWNLOAD_FAILED: ("❗", "回放下载失败", FOOTER_DOWNLOAD),
    EventKind.PRUNE_FAILED: ("⚠️", "旧录播没能删除", FOOTER_DOWNLOAD),
    EventKind.TRANSCRIBE_FAILED: ("❗", "字幕转写失败", FOOTER_DOWNLOAD),
    EventKind.TRANSLATE_FAILED: ("❗", "中文字幕翻译失败", FOOTER_DOWNLOAD),
    EventKind.ANALYZED: ("🚦", "路灯表已生成，精华段落：", FOOTER_HIGHLIGHTS),
    EventKind.ANALYZE_FAILED: ("❗", "路灯表分析失败", FOOTER_HIGHLIGHTS),
    EventKind.UPLOADED: ("📤", "路灯表已上传腾讯文档", FOOTER_HIGHLIGHTS),
    EventKind.UPLOAD_FAILED: ("❗", "路灯表上传腾讯文档失败", FOOTER_HIGHLIGHTS),
    EventKind.READY: ("✅", "全部处理完成", FOOTER_DOWNLOAD),
    EventKind.DRIVE_UPLOADED: ("☁️", "网盘上传完成", FOOTER_DOWNLOAD),
    EventKind.DRIVE_FAILED: ("❗", "网盘上传失败", FOOTER_DOWNLOAD),
    EventKind.DRIVE_AUTHORIZATION_LAPSED: ("⚠️", "网盘授权失效", FOOTER_DOWNLOAD),
}
# The messages that show the live's cover. The other pipeline messages stay text: a cover
# on every message of one live would flood the group.
COVER_KINDS = {EventKind.UPCOMING, EventKind.LIVE, EventKind.DOWNLOADED, EventKind.READY}
KIND_LABELS = {"upcoming": "直播预告", "live": "开播", "offline": "下播", "downloaded": "录播已下载", "replay_unpublished": "回放未公开",
               "prune_failed": "旧录播没能删除", "uploaded": "上传完成",
               "analyzed": "路灯表已生成", "download_failed": "下载失败", "transcribe_failed": "转写失败",
               "translate_failed": "翻译失败", "analyze_failed": "分析失败", "upload_failed": "上传失败",
               "ready": "全部处理完成", "drive_uploaded": "网盘上传完成",
               "drive_failed": "网盘上传失败", "drive_authorization_lapsed": "网盘授权失效"}


class QQError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class QQSettings:
    app_id: str
    secret: str = field(repr=False)


# ---- groups ----

SWITCHES = ("group_files_to_drive", "group_file_notices")  # a group's two group-file switches


def decode_groups(raw: Any) -> dict[str, dict[str, Any]]:
    """The file's list of groups, keyed by openid; raises on anything else."""
    for g in expect_list(raw, "groups"):
        if not (isinstance(g, dict) and isinstance(g.get("openid"), str)):
            raise ValueError(f"not a group (needs an openid): {g!r}")
        if bad := [k for k in SWITCHES if not isinstance(g.get(k, False), bool)]:
            raise ValueError(f"not a group ({', '.join(bad)} must be true or false): {g!r}")
        if SHEET in g and SourceRecordSheet.of(g) is None:
            raise ValueError(f"not a group ({SHEET} must be {{file_id, tab_id}}): {g!r}")
    return {g["openid"]: g for g in raw}


SHEET = "source_record_sheet"  # a group's key for its source record sheet; absent: none


@dataclass(frozen=True)
class SourceRecordSheet:
    """A group's source record sheet (录播源记录表): one tab of a Tencent Docs spreadsheet,
    saved with the group as {"file_id": ..., "tab_id": ...}."""

    file_id: str
    tab_id: str

    @classmethod
    def of(cls, group: dict[str, Any]) -> SourceRecordSheet | None:
        """The group's sheet; None when it has none or its saved value is not one."""
        saved = group.get(SHEET)
        if not (isinstance(saved, dict) and set(saved) == {"file_id", "tab_id"}
                and all(isinstance(v, str) and v for v in saved.values())):
            return None
        return cls(saved["file_id"], saved["tab_id"])


@dataclass(frozen=True)
class RecordingGroup:
    """A group whose source record sheet gets a streamer's lives (QQGroups.recording)."""

    openid: str
    name: str
    sheet: SourceRecordSheet


def group_folder(name: str) -> str:
    """The group folder a group's name gives: the name itself, one folder at the top of the
    drive. Raises ValueError with the reason, in Chinese, when the name cannot be one: it
    is blank, holds a slash of either kind, or fails the roster's drive-folder check."""
    name = name.strip()
    if not name:
        raise ValueError("群名是空的：群文件会放进以群名命名的网盘文件夹，先填群名")
    if "/" in name or "\\" in name:
        raise ValueError("群名不能当网盘文件夹名：不能含有 / 或 \\")
    try:
        return drive_folder_as_typed(name)
    except ValueError as e:
        raise ValueError(f"群名不能当网盘文件夹名：{e}") from None


@dataclass(frozen=True)
class GroupFiles:
    """What the group-files module needs of one group: whether its files go to the drive,
    whether it is told about them, and its group folder. Both switches read as off for a
    group marked as removed, and for a group saved before the switches existed."""

    to_drive: bool
    notices: bool
    folder: str  # "" when the name cannot be a group folder (then `to_drive` is never saved on)

    @classmethod
    def of(cls, group: dict[str, Any]) -> GroupFiles:
        present = group.get("status") != "removed"
        try:
            folder = group_folder(group.get("name") or "")
        except ValueError:
            folder = ""
        return cls(to_drive=present and bool(group.get("group_files_to_drive")) and bool(folder),
                   notices=present and bool(group.get("group_file_notices")), folder=folder)


class QQGroups:
    """The groups the bot knows (data/qq_groups.json), keyed by group openid.

    A group: {openid, name, enabled, streamers: [streamer name: a person, every platform;
    older saves hold channel ids, still honoured], kinds: [event kind],
    status: "active" | "removed" (bot kicked) | "muted" (proactive messages turned off),
    added_at, group_files_to_drive, group_file_notices, source_record_sheet: {file_id, tab_id}
    (absent: none)}. New groups start switched off with no streamers. The source record sheet
    gets the ticked streamers' lives whatever `enabled` and the kinds say (see `recording`).
    The two group-file switches don't depend on `enabled`, the streamers or
    the kinds; a group saved before they existed has neither and reads as off and off
    (read them through `group_files`).

    Operator data: a damaged file raises StateFileError (the service refuses to start)
    instead of wiping the subscriptions; a missing one means no groups yet.
    """

    def __init__(self, path: Path):
        self._file: StateFile[dict[str, dict[str, Any]]] = StateFile(
            path, default=dict, policy="strict", decode=decode_groups,
            encode=lambda groups: list(groups.values()))
        self._groups = self._file.load()

    @property
    def path(self) -> Path:
        return self._file.path

    def save(self) -> None:
        self._file.save(self._groups)

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
             "group_files_to_drive": False, "group_file_notices": False,
             "status": "active", "added_at": datetime.now(timezone.utc).isoformat()}
        self._groups[openid] = g
        self.save()
        return dict(g), True

    def group_files(self, openid: str) -> GroupFiles | None:
        """The group's group-file switches and folder; None for a group the bot doesn't know."""
        g = self._groups.get(openid)
        return GroupFiles.of(g) if g else None

    def update(self, openid: str, source_record_sheet: SourceRecordSheet | str | None = None,
               **fields: Any) -> dict[str, Any]:
        """Change a group's settings; None values are left alone, `source_record_sheet` ""
        removes the group's sheet. Raises ValueError (nothing saved) when its files would go
        to the drive while its name cannot be their folder."""
        g = self._groups[openid]
        new = g | {k: v for k, v in fields.items() if v is not None}
        if new.get("group_files_to_drive"):
            group_folder(new.get("name") or "")
        if source_record_sheet == "":
            new.pop(SHEET, None)
        elif isinstance(source_record_sheet, SourceRecordSheet):
            new[SHEET] = {"file_id": source_record_sheet.file_id, "tab_id": source_record_sheet.tab_id}
        g.clear()
        g.update(new)
        self.save()
        return dict(g)

    def remove(self, openid: str) -> None:
        del self._groups[openid]
        self.save()

    def recording(self, streamer: Streamer) -> list[RecordingGroup]:
        """The groups whose source record sheet gets this streamer's lives: the sheet is set,
        the streamer is ticked (by person; an older save's channel id counts for that channel)
        and the bot was not removed. Not `enabled`, not the kinds: those are for messages."""
        found = []
        for g in self._groups.values():
            ticked = g.get("streamers") or []
            sheet = SourceRecordSheet.of(g)
            if sheet and g.get("status") != "removed" and (streamer.name in ticked or streamer.id in ticked):
                found.append(RecordingGroup(g["openid"], g.get("name") or "", sheet))
        return found

    def targets(self, event: StreamEvent) -> list[dict[str, Any]]:
        s, kind = event.status.streamer, str(event.kind)
        return [dict(g) for g in self._groups.values()
                if g.get("enabled") and g.get("status") == "active" and kind in g.get("kinds", [])
                and (s.name in g.get("streamers", []) or s.id in g.get("streamers", []))]


# ---- messages ----

def escape(text: str) -> str:
    """Plain text inside QQ markdown, shown as that text. QQ's markdown cannot be trusted
    with backslash escapes (`\\[ ... \\]` renders as a display-math block, `\\`` as a lone
    backtick), so the characters that start markdown or math become their full-width
    forms instead, and backticks (code formatting meant for Discord) are dropped."""
    return (text or "").translate(PLAIN_TEXT)


PLAIN_TEXT = str.maketrans({"\\": "＼", "[": "［", "]": "］", "*": "＊", "_": "＿", "~": "～", "#": "＃",
                            ">": "＞", "|": "｜", "$": "＄", "`": None})


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
    """The markdown text of one event. `cover`: a qqbot-media markdown image line, put
    right after the headline."""
    s = event.status
    name, title = escape(s.streamer.name), escape(s.title or "")
    if event.kind is EventKind.LIVE:
        lines = [f"🔴 **{name}** 开播了！"]
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
    elif event.kind is EventKind.UPCOMING:
        lines = [f"📅 **{name}** 发布了直播预告", f"{name} · {s.streamer.platform}", title]
        if s.started_at:  # the scheduled start
            at = s.started_at.astimezone(LOCAL_TZ)
            # The year only when it is not this one (a permanent waiting room is a year out).
            year = "" if at.year == s.checked_at.astimezone(LOCAL_TZ).year else f"{at.year}年"
            lines.append(f"🕒 预定 {year}{at.month}月{at.day}日 {at:%H:%M} 开播")
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
        lines += [escape(line) for line in (event.detail or "").splitlines()]
        lines += [f"[{escape(x.label)}]({x.url})" for x in event.links]
        lines.append(footer)
    if cover:
        lines.insert(1, cover)
    return "\n".join(line for line in lines if line)


@dataclass
class Cover:
    """A live's cover uploaded to the bot's media library."""

    data: bytes  # the image, to upload it again if QQ refuses the reference
    line: str  # the markdown image line with its qqbot-media:// reference


def live_key(status: StreamStatus) -> tuple[str, str] | None:
    """What a live's cover is remembered by; None when the platform gave no stream id
    (then every message fetches the cover itself)."""
    return (status.streamer.id, status.stream_id) if status.stream_id else None


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


# ---- sending with tries ----

TRIES = 2  # tries of one message
RETRY_PAUSE = 2.0  # seconds between two tries


def refused_by_rule(e: Exception) -> bool:
    """QQ refused by one of its rules (a 40xxxxxx code, e.g. proactive messages off):
    trying again won't help."""
    return isinstance(e, QQError) and bool(e.code) and 40000000 <= int(e.code) < 50000000


async def send_with_tries(api: QQApi, group: str, text: str, tries: int = TRIES, msg_id: str | None = None) -> None:
    """Send `text` to `group` (a reply to `msg_id` when given), with `tries` tries: a network
    error, or an error QQ gives that is not one of its rules, is tried again after a short
    pause. Raises the last error."""
    for attempt in range(tries):
        try:
            await api.send_markdown(group, text, msg_id=msg_id)
            return
        except (QQError, httpx.HTTPError) as e:
            if refused_by_rule(e) or attempt + 1 >= tries:
                raise
        await asyncio.sleep(RETRY_PAUSE)


# ---- notifier ----

class QQNotifier(Notifier):
    name = "qq"
    logs_itself = True  # one history entry per group

    def __init__(self, settings: QQSettings, groups: QQGroups, client: httpx.AsyncClient | None = None,
                 history: NotifyHistory | None = None, gap: float = 1.0, tries: int = TRIES):
        self.settings, self.groups = settings, groups
        self.client = client or httpx.AsyncClient(timeout=30)
        self.api = QQApi(settings, self.client)
        self.history = history or NotifyHistory()
        self.gap, self.tries = gap, tries  # seconds between groups; tries per group
        # (streamer id, stream id) -> the live's uploaded cover, newest last; kept while
        # the service runs, so after a restart a cover is fetched and uploaded again.
        self._covers: OrderedDict[tuple[str, str], Cover] = OrderedDict()

    async def send(self, event: StreamEvent) -> None:
        targets = self.groups.targets(event)
        if not targets:
            return
        # Going live fetches the cover again: the 直播预告 often showed a placeholder.
        cover, reused = (await self.cover(event.status, fresh=event.kind is EventKind.LIVE)
                         if event.kind in COVER_KINDS else (None, False))
        text = message(event, cover.line if cover else None)
        summary = text.splitlines()[0].replace("**", "")
        for i, g in enumerate(targets):
            if i and self.gap:
                await asyncio.sleep(self.gap)
            if await self.deliver(g, text, summary, quiet=reused) or cover is None or not reused:
                continue
            # QQ may not take a reference from an earlier message (never checked live):
            # upload the kept image again, or send the text alone.
            log.info("qq: a reused cover was refused; uploading it again")
            cover, reused = await self.upload_cover(live_key(event.status), cover.data), False
            text = message(event, cover.line if cover else None)
            await self.deliver(g, text, summary)

    async def deliver(self, group: dict[str, Any], text: str, summary: str, quiet: bool = False) -> bool:
        """Send to one group, retrying a little; logs and records the outcome.
        `quiet`: a failure is neither logged nor recorded (the caller tries again)."""
        try:
            await send_with_tries(self.api, group["openid"], text, self.tries)
            self.history.add("qq", group["openid"], summary, True)
            return True
        except (QQError, httpx.HTTPError) as e:
            error = str(e) or type(e).__name__
        if quiet:
            return False
        log.warning("qq: %s not delivered to %s: %s", summary, group.get("name") or group["openid"], error)
        self.history.add("qq", group["openid"], summary, False, error)
        return False

    async def cover(self, status: StreamStatus, fresh: bool = False) -> tuple[Cover | None, bool]:
        """The live's cover in the media library (None: the message is text only), and
        whether it was uploaded for an earlier message. A live's cover is uploaded once
        and reused for its later messages: by then its address may show another picture
        (a Twitch preview shows whatever the channel shows now). `fresh`: fetch it again
        all the same and keep the new one (a scheduled live's cover is often set late);
        when that fails, the cover kept for the live is used after all."""
        key = live_key(status)
        known = self._covers.get(key) if key else None
        if known and not fresh:
            self._covers.move_to_end(key)
            return known, True
        cover = await self.fetch_cover(key, status.thumbnail) if status.thumbnail else None
        if cover is None and known:
            self._covers[key] = known  # a failed upload forgot it
            self._covers.move_to_end(key)
            return known, True
        return cover, False

    async def fetch_cover(self, key: tuple[str, str] | None, address: str) -> Cover | None:
        """The picture at `address`, uploaded and kept for the live `key`; None when it cannot
        be fetched or uploaded."""
        try:
            r = await self.client.get(address, follow_redirects=True, timeout=20)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("qq: cover not used (%s)", e)
            return None
        return await self.upload_cover(key, r.content)

    async def upload_cover(self, key: tuple[str, str] | None, data: bytes) -> Cover | None:
        """Upload a cover's image and keep it for the live `key` (None: not kept). A
        failed upload forgets what was kept for that live."""
        try:
            ref = await self.api.upload_image(data, "image/png" if data[:4] == b"\x89PNG" else "image/jpeg")
        except (QQError, httpx.HTTPError, KeyError) as e:
            log.warning("qq: cover not used (%s)", e)
            if key:
                self._covers.pop(key, None)
            return None
        w, h = image_size(data) or (480, 270)
        scale = min(1.0, 480 / w) if w else 1.0
        cover = Cover(data, f"![封面 #{round(w * scale)}px #{round(h * scale)}px]({ref})")
        if key:
            self._covers[key] = cover
            while len(self._covers) > COVERS_KEPT:
                self._covers.popitem(last=False)
        return cover

    async def aclose(self) -> None:
        await self.client.aclose()
