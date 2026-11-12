"""The 通知 page: channel status, QQ groups and what each subscribes to, recent messages.

    GET    /notify                      the page
    GET    /api/notify                  everything the page shows
    PATCH  /api/notify/groups?id=<openid>   {name?, enabled?, streamers?, kinds?}
    DELETE /api/notify/groups?id=<openid>
    POST   /api/notify/test             {target: "discord" | <group openid>}
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.notify import EventKind, LiveNotifier
from backend.notify.qq import KIND_LABELS, QQError
from backend.notify.qq_gateway import QQGateway

from .__main__ import REPO_ROOT
from .roster import Roster, entry_id

PAGE = REPO_ROOT / "frontend" / "notify.html"
TEST_TEXT = "🧪 测试消息：切片小助手的通知正常 ✅"


class GroupSettings(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    streamers: list[str] | None = None
    kinds: list[str] | None = None


class TestTarget(BaseModel):
    target: str


def create_notify_router(notifier: LiveNotifier | None, roster: Roster, gateway: QQGateway | None) -> APIRouter:
    router = APIRouter()
    qq = notifier.qq if notifier else None
    discord = next((n for n in notifier.notifiers if n.name == "discord"), None) if notifier else None

    def groups():
        if not qq:
            raise HTTPException(404, "QQ 机器人没有设定（.env 里的 QQ_BOT_APP_ID / QQ_BOT_SECRET）")
        return qq.groups

    @router.get("/notify")
    def page() -> FileResponse:
        return FileResponse(PAGE)

    @router.get("/api/notify")
    def status() -> dict[str, Any]:
        return {
            "discord": {"configured": discord is not None},
            "qq": {"configured": qq is not None,
                   "online": bool(gateway and gateway.online),
                   "bot_name": gateway.bot_name if gateway else "",
                   "last_error": gateway.last_error if gateway else ""},
            "groups": qq.groups.all() if qq else [],
            "streamers": [{"id": entry_id(e), "name": e.get("name") or e["channel"], "platform": e["platform"]}
                          for e in roster.entries],
            "kinds": [{"id": str(k), "label": KIND_LABELS.get(str(k), str(k))} for k in EventKind],
            "log": notifier.history.entries() if notifier else [],
        }

    @router.patch("/api/notify/groups")
    def update_group(id: str, body: GroupSettings) -> dict[str, Any]:
        store = groups()
        if not store.get(id):
            raise HTTPException(404, "没有这个群")
        known_kinds = {str(k) for k in EventKind}
        if body.kinds is not None and not set(body.kinds) <= known_kinds:
            raise HTTPException(422, f"不认识的通知类型：{', '.join(sorted(set(body.kinds) - known_kinds))}")
        known = {entry_id(e) for e in roster.entries}
        if body.streamers is not None and not set(body.streamers) <= known:
            raise HTTPException(422, "名单里没有这个主播")
        name = body.name.strip()[:40] if body.name is not None else None
        return store.update(id, name=name, enabled=body.enabled, streamers=body.streamers, kinds=body.kinds)

    @router.delete("/api/notify/groups")
    def delete_group(id: str) -> dict[str, Any]:
        store = groups()
        if not store.get(id):
            raise HTTPException(404, "没有这个群")
        store.remove(id)
        return {"id": id}

    @router.post("/api/notify/test")
    async def test(body: TestTarget) -> dict[str, Any]:
        if body.target == "discord":
            if not discord or not notifier:
                raise HTTPException(404, "Discord 没有设定")
            try:
                await discord.alert(TEST_TEXT)  # type: ignore[attr-defined]
            except httpx.HTTPError as e:
                notifier.history.add("discord", "测试", TEST_TEXT, False, str(e))
                return {"ok": False, "error": str(e)}
            notifier.history.add("discord", "测试", TEST_TEXT, True)
            return {"ok": True}
        store = groups()
        if not store.get(body.target) or not qq:
            raise HTTPException(404, "没有这个群")
        try:
            await qq.api.send_markdown(body.target, TEST_TEXT)
        except (QQError, httpx.HTTPError) as e:
            qq.history.add("qq", body.target, TEST_TEXT, False, str(e))
            return {"ok": False, "error": str(e)}
        qq.history.add("qq", body.target, TEST_TEXT, True)
        return {"ok": True}

    return router
