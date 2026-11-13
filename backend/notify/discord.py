"""Discord notifications via a channel webhook (no bot needed).

Create one in Discord: channel settings → Integrations → Webhooks → New Webhook →
Copy Webhook URL, then put it in .env as DISCORD_WEBHOOK_URL.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from .base import EventKind, Notifier, PermanentNotifyError, StreamEvent

PLATFORM_COLORS = {"youtube": 0xFF0000, "twitch": 0x9146FF, "twitcasting": 0x3381FF}
DEFAULT_COLOR = 0xE11D48
OFFLINE_COLOR = 0x6B7280
DOWNLOADED_COLOR = 0x16A34A
FAILED_COLOR = 0xDC2626
WAITING_COLOR = OFFLINE_COLOR
FOOTER_LIVE = "切片小助手 直播通知"
FOOTER_DOWNLOAD = "切片小助手 自动下载"
FOOTER_HIGHLIGHTS = "切片小助手 自动路灯"  # the 路灯表: analysis and upload
HIGHLIGHT_KINDS = {EventKind.ANALYZED, EventKind.ANALYZE_FAILED, EventKind.UPLOADED, EventKind.UPLOAD_FAILED}
# Pipeline events (backend.downloader.auto): icon and message text.
PIPELINE_TEXT = {
    EventKind.DOWNLOADED: ("📥", "回放已下载"),
    EventKind.REPLAY_UNPUBLISHED: ("⏳", "回放未公开"),
    EventKind.DOWNLOAD_FAILED: ("❗", "回放下载失败"),
    EventKind.TRANSCRIBE_FAILED: ("❗", "字幕转写失败"),
    EventKind.ANALYZED: ("🚦", "路灯表已生成，精华段落："),
    EventKind.ANALYZE_FAILED: ("❗", "路灯表分析失败"),
    EventKind.TRANSLATE_FAILED: ("❗", "中文字幕翻译失败"),
    EventKind.UPLOADED: ("📤", "路灯表已上传腾讯文档"),
    EventKind.UPLOAD_FAILED: ("❗", "路灯表上传腾讯文档失败"),
    EventKind.READY: ("✅", "全部处理完成"),
}


def _duration(start: datetime, end: datetime) -> str:
    minutes = max(0, int((end - start).total_seconds() // 60))
    h, m = divmod(minutes, 60)
    return f"{h} 小时 {m} 分" if h else f"{m} 分钟"


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, webhook_url: str, mention: str | None = None, client: httpx.AsyncClient | None = None):
        self.webhook_url = webhook_url
        self.mention = (mention or "").strip()  # pinged on go-live only
        self.client = client or httpx.AsyncClient(timeout=15)

    def build_payload(self, event: StreamEvent) -> dict[str, Any]:
        if event.kind in PIPELINE_TEXT:
            return self.download_payload(event)
        status = event.status
        s = status.streamer
        live = event.kind is EventKind.LIVE
        embed: dict[str, Any] = {
            "title": (status.title or ("直播中" if live else "直播已结束"))[:256],
            "color": PLATFORM_COLORS.get(s.platform, DEFAULT_COLOR) if live else OFFLINE_COLOR,
            "author": {"name": f"{s.name} · {s.platform}"},
            "footer": {"text": FOOTER_LIVE},
        }
        if status.url:
            embed["url"] = status.url
            embed["author"]["url"] = status.url
        if status.avatar:
            embed["author"]["icon_url"] = status.avatar

        fields = []
        if live:
            if status.thumbnail:
                embed["image"] = {"url": status.thumbnail}
            if status.started_at:
                embed["timestamp"] = status.started_at.isoformat()
            if status.viewers is not None:
                fields.append({"name": "观看人数", "value": f"{status.viewers:,}", "inline": True})
            content = f"🔴 **{s.name}** 开播了！"
            if self.mention:
                content = f"{self.mention} {content}"
        else:
            if status.thumbnail:
                embed["thumbnail"] = {"url": status.thumbnail}
            if event.ended_at:
                embed["timestamp"] = event.ended_at.isoformat()
                if status.started_at:
                    fields.append({"name": "直播时长", "value": _duration(status.started_at, event.ended_at), "inline": True})
            content = f"⚫ **{s.name}** 下播了"
        if fields:
            embed["fields"] = fields

        return {
            "content": content,
            "embeds": [embed],
            # Only ping on go-live, and only what `mention` asks for.
            "allowed_mentions": {"parse": ["everyone", "roles", "users"] if live and self.mention else []},
        }

    def download_payload(self, event: StreamEvent) -> dict[str, Any]:
        status = event.status
        s = status.streamer
        ok = event.kind in (EventKind.DOWNLOADED, EventKind.ANALYZED, EventKind.UPLOADED, EventKind.READY)
        waiting = event.kind is EventKind.REPLAY_UNPUBLISHED  # neither done nor failed
        embed: dict[str, Any] = {
            "title": (status.title or "直播回放")[:256],
            "color": WAITING_COLOR if waiting else DOWNLOADED_COLOR if ok else FAILED_COLOR,
            "author": {"name": f"{s.name} · {s.platform}"},
            "footer": {"text": FOOTER_HIGHLIGHTS if event.kind in HIGHLIGHT_KINDS else FOOTER_DOWNLOAD},
        }
        if status.url:
            embed["url"] = status.url
        if status.avatar:
            embed["author"]["icon_url"] = status.avatar
        if event.detail:
            embed["description"] = event.detail[:4000]
        icon, verb = PIPELINE_TEXT[event.kind]
        return {
            "content": f"{icon} **{s.name}** {verb}",
            "embeds": [embed],
            "allowed_mentions": {"parse": []},
        }

    async def send(self, event: StreamEvent) -> None:
        """Network errors and 5xx raise normally (the queue retries them); a rejected
        webhook or payload (4xx) raises PermanentNotifyError."""
        payload = self.build_payload(event)
        for _ in range(3):
            resp = await self.client.post(self.webhook_url, json=payload)
            if resp.status_code == 429:  # rate limited: wait as told, then retry
                try:
                    retry_after = float(resp.json().get("retry_after", 1))
                except ValueError:
                    retry_after = 1.0
                await asyncio.sleep(min(retry_after, 60))
                continue
            if 400 <= resp.status_code < 500:
                raise PermanentNotifyError(f"Discord rejected the message: HTTP {resp.status_code} {resp.text[:200]}")
            resp.raise_for_status()
            return
        raise RuntimeError("Discord kept rate-limiting the webhook")

    async def alert(self, text: str) -> None:
        """A plain warning for whoever runs this (e.g. the QQ bot is offline)."""
        resp = await self.client.post(self.webhook_url, json={"content": f"⚠️ {text}", "allowed_mentions": {"parse": []}})
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self.client.aclose()
