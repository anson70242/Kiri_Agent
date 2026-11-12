"""QQ bot gateway: groups register themselves (bot added / @-mentioned), get removed or
muted, and the connection keeps coming back. Fake QQ API + fake WebSocket."""

from __future__ import annotations

import asyncio
import json

from backend.notify.qq import QQGroups, QQSettings
from backend.notify.qq_gateway import QQGateway

from .test_qq import SECRET, FakeQQ


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def gateway(tmp_path, fake, **kw):
    groups = QQGroups(tmp_path / "qq_groups.json")
    alerts: list[str] = []
    gw = QQGateway(QQSettings("APP1", SECRET), groups, client=fake.client(),
                   names=lambda: {"youtube:@a": "A", "twitch:b": "B"}, alert=alerts.append, **kw)
    return gw, groups, alerts


def dispatch(gw, t, d, event_id="EV1"):
    asyncio.run(gw.handle({"op": 0, "t": t, "id": event_id, "d": d}))


def test_a_mention_registers_the_group_and_replies_with_its_subscriptions(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)

    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "MSG1", "content": " 你好"})

    g = groups.get("G1")
    assert g["enabled"] is False and g["streamers"] == [] and g["status"] == "active"
    (group, body), = fake.messages
    assert group == "G1" and body["msg_id"] == "MSG1"  # a reply to the mention
    assert body["markdown"]["content"].startswith("已登记本群 ✅")
    assert "本群已订阅：（还没有）" in body["markdown"]["content"]

    groups.update("G1", streamers=["youtube:@a", "twitch:b"], enabled=True)
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "MSG2", "content": ""})
    text = fake.messages[-1][1]["markdown"]["content"]
    assert "本群已订阅：A、B" in text and "开播" in text


def test_being_added_registers_the_group(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    dispatch(gw, "GROUP_ADD_ROBOT", {"group_openid": "G2", "op_member_openid": "M1"}, event_id="EV9")
    assert groups.get("G2") is not None
    (_, body), = fake.messages
    assert body["event_id"] == "EV9"


def test_removed_or_muted_groups_stop_and_raise_one_alert(tmp_path):
    fake = FakeQQ()
    gw, groups, alerts = gateway(tmp_path, fake)
    groups.register("G1")
    groups.update("G1", name="章鱼烧")
    groups.register("G2")

    dispatch(gw, "GROUP_DEL_ROBOT", {"group_openid": "G1"})
    dispatch(gw, "GROUP_MSG_REJECT", {"group_openid": "G2"})
    assert groups.get("G1")["status"] == "removed" and groups.get("G2")["status"] == "muted"
    assert len(alerts) == 2 and "章鱼烧" in alerts[0]
    dispatch(gw, "GROUP_MSG_RECEIVE", {"group_openid": "G2"})
    assert groups.get("G2")["status"] == "active"


class FakeSocket:
    """Plays the gateway's side of one connection, then drops it."""

    def __init__(self, ready=True):
        self.sent: list[dict] = []
        self.inbox = [{"op": 10, "d": {"heartbeat_interval": 41250}}]
        if ready:
            self.inbox.append({"op": 0, "t": "READY", "s": 1, "d": {"user": {"username": "测试员"}}})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if self.inbox:
            return json.dumps(self.inbox.pop(0))
        raise ConnectionError("connection dropped")


def test_the_bot_identifies_goes_online_and_reconnects(tmp_path):
    fake = FakeQQ()
    made = [FakeSocket(), FakeSocket()]
    sockets = list(made)
    clock = Clock()

    async def no_sleep(_):
        clock.t += 5

    def connect(url):
        assert url == "wss://gateway.example"
        if not sockets:
            raise asyncio.CancelledError  # end of the test
        return sockets.pop(0)

    fake_gateway(fake)
    gw, _, alerts = gateway(tmp_path, fake, connect=connect, clock=clock, sleep=no_sleep)
    try:
        asyncio.run(gw.run())
    except asyncio.CancelledError:
        pass
    assert gw.bot_name == "测试员"
    identify = [m for sock in made for m in sock.sent if m["op"] == 2]
    assert len(identify) == 2 and identify[0]["d"]["token"] == "QQBot TOKEN1"
    assert identify[0]["d"]["intents"] & (1 << 25)  # group events
    assert alerts == []  # short drops are not worth a message


def test_a_long_outage_alerts_once(tmp_path):
    fake = FakeQQ()
    clock = Clock()
    tries = {"n": 0}

    async def sleep(seconds):
        clock.t += seconds

    def connect(url):
        tries["n"] += 1
        if tries["n"] > 40:
            raise asyncio.CancelledError
        raise OSError("network unreachable")

    fake_gateway(fake)
    gw, _, alerts = gateway(tmp_path, fake, connect=connect, clock=clock, sleep=sleep)
    try:
        asyncio.run(gw.run())
    except asyncio.CancelledError:
        pass
    assert clock.t > 600 and not gw.online
    assert len(alerts) == 1 and "QQ" in alerts[0]
    assert "network unreachable" in gw.last_error


def fake_gateway(fake: FakeQQ) -> None:
    real = fake.handle

    def handle(request):
        if request.url.path == "/gateway":
            return __import__("httpx").Response(200, json={"url": "wss://gateway.example"})
        return real(request)

    fake.handle = handle

