"""The QQ bot's event connection (WebSocket gateway): keeps the bot online and learns
which groups it is in.

QQ has no "list my groups" API: a group's openid only arrives in events. So:
- GROUP_ADD_ROBOT (bot added) / a mention of the bot: register the group (new ones start
  switched off) and reply with what it is subscribed to.
- A mention arrives as GROUP_AT_MESSAGE_CREATE in a group that sends the bot only its
  mentions, and as an ordinary GROUP_MESSAGE_CREATE marking the bot in `mentions` in a
  group that sends it everything ("接收所有消息"). Both are handled alike: after a group's
  first mention, the mention's first word is a command (`COMMANDS`): 订阅 (the status,
  for the group owner / admins only, so not every member can make the bot repeat it) or
  帮助 (the commands, for anyone); anything else gets the help.
- Any other message is never answered and registers nothing. Nothing of a message's text
  is logged: in a group that sends everything, that is the members' chat.
- Every `file` attachment of a group message, mentioned or not, is handed to the group-files
  module (`take_group_file`) with the group, the message's id and time, and nothing else of
  the message; the module decides by the group's switch (backend.group_files).
- GROUP_DEL_ROBOT (bot removed) -> "removed"; GROUP_MSG_REJECT (proactive messages
  turned off) -> "muted"; both stop deliveries and raise one alert (Discord).
  GROUP_MSG_RECEIVE (turned back on) -> "active".

The connection drops now and then; it reconnects on its own. Only an outage longer than
`alert_after` (10 min) raises an alert, once, until the bot is back online.

Protocol (checked live, 2026-11-12): GET /gateway -> {url}; the server says hello (op 10,
heartbeat_interval ms); we identify (op 2, token "QQBot <access token>", intents with
1<<25 for group/C2C events); READY arrives as op 0; heartbeats are op 1 with the last seq.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from backend.group_files.rules import rule_lines

from .qq import KIND_LABELS, GroupFiles, QQApi, QQError, QQGroups, QQSettings

log = logging.getLogger(__name__)

GROUP_EVENTS = 1 << 25
MESSAGE_EVENTS = ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE")


PLATFORMS = {"youtube": "YouTube", "twitch": "Twitch"}


def subscribed_people(subs: list[str], names: dict[str, str]) -> list[str]:
    """Each subscribed person once, with their platforms: "森野鈴 (YouTube / Twitch)".
    `names`: channel id ("platform:channel") -> person. A subscription is a person's name
    (all their channels) or, from older saves, one channel id."""
    people: dict[str, list[str]] = {}
    for s in subs:
        person = names.get(s, s)
        channels = [s] if s in names else [i for i, n in names.items() if n == s]
        plats = people.setdefault(person, [])
        for i in channels:
            p = PLATFORMS.get(i.split(":", 1)[0], i.split(":", 1)[0])
            if p not in plats:
                plats.append(p)
    return [f"{person} ({' / '.join(plats)})" if plats else person for person, plats in people.items()]


def is_mention(t: str | None, d: dict[str, Any]) -> bool:
    """Whether the bot was mentioned. A group that sends the bot only its mentions sends
    GROUP_AT_MESSAGE_CREATE; a group that sends it everything sends an ordinary
    GROUP_MESSAGE_CREATE whose `mentions` marks the bot's own entry with `is_you`."""
    if t == "GROUP_AT_MESSAGE_CREATE":
        return True
    return t == "GROUP_MESSAGE_CREATE" and any(m.get("is_you") for m in d.get("mentions") or [])


MENTION_MARK = re.compile(r"<@[^>]*>")


def command_word(content: str | None) -> str:
    """A mention's command: the first word of its text once every `<@…>` is gone (an
    ordinary message's text starts with the mention itself; a mention event's has none)."""
    words = MENTION_MARK.sub(" ", content or "").split()
    return words[0] if words else ""


def default_connect(url: str):
    import websockets

    return websockets.connect(url, open_timeout=20)


class QQGateway:
    def __init__(self, settings: QQSettings, groups: QQGroups, *,
                 names: Callable[[], dict[str, str]],
                 alert: Callable[[str], Any],
                 # (group openid, message id, message time, attachment): the group-files module
                 take_group_file: Callable[[str, str, Any, dict[str, Any]], Any] | None = None,
                 client: httpx.AsyncClient | None = None,
                 connect: Callable[[str], Any] = default_connect,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 alert_after: float = 600):
        self.groups, self.names, self.alert = groups, names, alert
        self.take_group_file = take_group_file
        self.client = client or httpx.AsyncClient(timeout=30)
        self.api = QQApi(settings, self.client)
        self.connect, self.clock, self.sleep, self.alert_after = connect, clock, sleep, alert_after
        self.online = False
        self.bot_name = ""
        self.last_error = ""
        self.offline_since: float | None = None
        self.alerted = False

    # ---- events ----

    async def handle(self, payload: dict[str, Any]) -> None:
        """One dispatch (op 0) from the gateway."""
        t, d = payload.get("t"), payload.get("d") or {}
        group = d.get("group_openid")
        if t == "READY":
            self.bot_name = (d.get("user") or {}).get("username") or ""
            return
        if not group:
            return
        if t in MESSAGE_EVENTS:
            self._hand_over_files(group, d)
        if t == "GROUP_ADD_ROBOT":
            g, new = self.groups.register(group)
            log.info("qq: group %s %s", g.get("name") or group, "registered" if new else "added again")
            await self._reply(group, self.status_text(g), event_id=payload.get("id"))
        elif is_mention(t, d):
            await self._answer_mention(group, d)
        elif t == "GROUP_DEL_ROBOT":
            self._set_status(group, "removed", "机器人被移出了群")
        elif t == "GROUP_MSG_REJECT":
            self._set_status(group, "muted", "群里关闭了机器人主动发言")
        elif t == "GROUP_MSG_RECEIVE":
            if self.groups.get(group):
                self.groups.update(group, status="active")

    async def _answer_mention(self, group: str, d: dict[str, Any]) -> None:
        g, new = self.groups.register(group)
        if new:  # whatever the first mention says, it is told it is registered, as when the bot is added
            log.info("qq: group %s registered", g.get("name") or group)
            await self._reply(group, self.status_text(g), msg_id=d.get("id"))
            return
        command = COMMANDS.get(command_word(d.get("content"))) or COMMANDS[HELP]
        role = (d.get("author") or {}).get("member_role")
        if command.admins_only and role not in ("owner", "admin"):
            return  # anyone may @ the bot; not everyone may make it repeat a long answer
        log.info("qq: group %s mentioned", g.get("name") or group)  # never the text: it is the members' chat
        await self._reply(group, command.answer(self, g), msg_id=d.get("id"))

    def _hand_over_files(self, group: str, d: dict[str, Any]) -> None:
        """Each `file` attachment (a group file) to the group-files module; pictures, voice and
        video have other content types and stay here, like the rest of the message."""
        if not self.take_group_file:
            return
        for a in d.get("attachments") or []:
            if isinstance(a, dict) and a.get("content_type") == "file":
                try:
                    self.take_group_file(group, d.get("id") or "", d.get("timestamp"), dict(a))
                except Exception as e:  # noqa: BLE001 - a broken hand-over never stops the gateway
                    log.error("qq: a group file of %s was not taken: %s", group, type(e).__name__)

    async def _reply(self, group: str, content: str, **reply: Any) -> None:
        try:
            await self.api.send_markdown(group, content, **reply)
        except (QQError, httpx.HTTPError) as e:
            log.warning("qq: could not reply in %s: %s", group, e)

    def _set_status(self, group: str, status: str, why: str) -> None:
        g = self.groups.get(group) or self.groups.register(group)[0]
        self.groups.update(group, status=status)
        self.alert(f"QQ 群「{g.get('name') or group}」{why}，通知已暂停。")

    def status_text(self, g: dict[str, Any]) -> str:
        streamers = subscribed_people(g.get("streamers") or [], self.names())
        kinds = [KIND_LABELS.get(k, k) for k in g.get("kinds") or []]
        lines = ["已登记本群 ✅"]
        if streamers:  # one person a line: easier to read than a comma list
            lines += ["本群已订阅：", *(f"- {p}" for p in streamers)]
        else:
            lines.append("本群已订阅：（还没有）")
        if streamers:
            lines.append(f"通知类型：{'、'.join(kinds) if kinds else '（无）'}")
        if not g.get("enabled") or not streamers:
            lines.append("通知目前关闭，请到切片小助手的通知页面设定")
        return "\n".join(lines)

    def help_text(self, g: dict[str, Any]) -> str:
        lines = ["@我 并在后面写上指令：", *(f"- {word}：{c.does}" for word, c in COMMANDS.items())]
        # What a group's own features add to the help comes after the commands.
        files = GroupFiles.of(g)
        if files.to_drive:
            lines += ["", "本群的群文件按文件名自动存到夸克网盘：", *rule_lines(files.folder)]
        return "\n".join(lines)

    # ---- connection ----

    async def run(self) -> None:
        """Stay connected forever (cancel to stop)."""
        delay = 5.0
        while True:
            try:
                await self.session()
                delay = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure: reconnect
                self.last_error = str(e) or type(e).__name__
                log.warning("qq gateway: %s", self.last_error)
            self._went_offline()
            await self.sleep(delay)
            delay = min(delay * 2, 60.0)

    def _went_offline(self) -> None:
        self.online = False
        now = self.clock()
        if self.offline_since is None:
            self.offline_since = now
        if not self.alerted and now - self.offline_since >= self.alert_after:
            self.alerted = True
            self.alert(f"QQ 机器人已离线 {int((now - self.offline_since) // 60)} 分钟，QQ 通知暂停："
                       f"{self.last_error or '连不上'}")

    async def session(self) -> None:
        url = (await self.api.call("GET", "/gateway")).get("url")
        if not url:
            raise QQError("QQ gateway gave no url")
        async with self.connect(url) as ws:
            hello = json.loads(await ws.recv())
            interval = float((hello.get("d") or {}).get("heartbeat_interval") or 41250) / 1000
            await ws.send(json.dumps({"op": 2, "d": {"token": f"QQBot {await self.api.token()}",
                                                     "intents": GROUP_EVENTS, "shard": [0, 1]}}))
            seq: dict[str, int | None] = {"s": None}

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    await ws.send(json.dumps({"op": 1, "d": seq["s"]}))

            beat = asyncio.create_task(heartbeat())
            try:
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("s"):
                        seq["s"] = msg["s"]
                    op = msg.get("op")
                    if op == 0:
                        if msg.get("t") == "READY":
                            self.online, self.offline_since, self.alerted, self.last_error = True, None, False, ""
                            log.info("qq gateway: online as %s", (msg.get("d") or {}).get("user", {}).get("username"))
                        await self.handle(msg)
                    elif op in (7, 9):  # reconnect requested / invalid session
                        raise QQError(f"gateway asked to reconnect (op {op})")
            finally:
                beat.cancel()

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass(frozen=True)
class Command:
    """What a mention can ask for: its line in the help, its answer for the group, and
    whether only the group's owner and admins get that answer."""

    does: str
    answer: Callable[[QQGateway, dict[str, Any]], str]
    admins_only: bool = False


HELP = "帮助"

# word -> command, in the help's order. A new command is one more entry here.
COMMANDS: dict[str, Command] = {
    "订阅": Command("查看本群订阅的主播和通知类型（群主和管理员）", QQGateway.status_text, admins_only=True),
    HELP: Command("列出我能听懂的指令", QQGateway.help_text),
}
