"""Web dashboard for the live monitor.

    uv run -m backend.monitor.server            # http://127.0.0.1:8770
    uv run -m backend.monitor.server --port 9000
    uv run -m backend.monitor.server --dev --reload   # local preview (see scripts/pull-archive.ps1)

Polls in the background; the page reads /api/status.
"""

import argparse
import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.auth import auth_enabled, load_auth
from backend.downloader import load_settings
from backend.downloader.auto import attach
from backend.notify.qq_gateway import QQGateway

from . import StreamMonitor
from .__main__ import REPO_ROOT, load_config
from .archives import create_router
from .auth_api import install_auth, install_open_me
from .notify_api import create_notify_router
from .base import LiveState, Streamer
from .roster import RosterError, entry_id, load_roster

PAGE = REPO_ROOT / "frontend" / "monitor.html"
USERS_PAGE = REPO_ROOT / "frontend" / "users.html"
COMMON_JS = REPO_ROOT / "frontend" / "common.js"

ChannelCheck = Callable[[Streamer], Awaitable[str | None]]  # error message, or None if it exists


class NewStreamer(BaseModel):
    name: str = ""
    platform: str
    channel: str


class StreamerSettings(BaseModel):
    """The gear: per-streamer settings. Omitted fields stay as they are."""

    auto_download: bool | None = None
    keep: int | None = Field(None, ge=1, le=100)  # archives kept; oldest deleted first
    upload: bool | None = None  # upload the 路灯表 to Tencent Docs once it is ready
    upload_sheet: str | None = None  # docs.qq.com/sheet/... link or id; "" clears it


def create_app(config: Path, interval: float | None = None, dev: bool = False,
               check_channel: ChannelCheck | None = None) -> FastAPI:
    """`dev`: local preview of the pages. Still shows live status, but never downloads,
    transcribes, analyzes or sends Discord messages.
    `check_channel`: replaces the real "does this channel exist" check (tests).
    Login is on when the config has `"auth": {"enabled": true}` (never in dev mode)."""
    _, cfg_interval = load_config(config)
    interval = interval or cfg_interval
    roster = load_roster(config)
    mon = StreamMonitor(roster.streamers())
    notifier, auto = (None, None) if dev else attach(config, mon)
    if dev:
        print("Dev mode: no auto-download, no notifications")
    elif notifier and notifier.notifiers:
        print("Notify: " + ", ".join(n.name for n in notifier.notifiers))
    else:
        print("Notify: off (set DISCORD_WEBHOOK_URL in .env to enable)")
    print(f"Auto-download: {len(auto.keep)} streamer(s)" if auto else "Auto-download: off")
    # The QQ bot's event connection: keeps it online and learns which groups it is in.
    gateway = QQGateway(
        notifier.qq.settings, notifier.qq.groups, alert=notifier.alert,
        names=lambda: {entry_id(e): e.get("name") or e["channel"] for e in roster.entries},
    ) if notifier and notifier.qq else None

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(mon.run(interval))
        auto_task = asyncio.create_task(auto.run()) if auto else None
        qq_task = asyncio.create_task(gateway.run()) if gateway else None
        yield
        task.cancel()
        if auto_task:
            auto_task.cancel()
        if qq_task:
            qq_task.cancel()
            await gateway.aclose()
        await mon.aclose()
        if notifier:
            await notifier.aclose()

    app = FastAPI(title="Kiri Live Monitor", lifespan=lifespan)
    app.state.notifier = notifier
    app.state.auto = auto
    if auth_enabled(config) and not dev:
        install_auth(app, load_auth(config))
        print("Login: on")
    else:
        install_open_me(app)
        print("Login: off")
    app.include_router(create_router(load_settings(config)[0].output_dir))
    app.include_router(create_notify_router(notifier, roster, gateway))

    def snapshot() -> dict:
        rows = []
        for s in mon.streamers:
            st = mon.statuses.get(s)
            rows.append(
                st.to_dict()
                if st
                else {"name": s.name, "platform": s.platform, "channel": s.channel, "state": "checking"}
            )
        return {
            "interval": interval,
            "last_refresh": mon.last_refresh.isoformat() if mon.last_refresh else None,
            "streamers": rows,
        }

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(PAGE)

    @app.get("/common.js", include_in_schema=False)
    async def common_js() -> FileResponse:
        """Shared page script: who is logged in, role-based UI, 401 -> login."""
        return FileResponse(COMMON_JS, media_type="text/javascript")

    @app.get("/users", include_in_schema=False)
    async def users_page() -> FileResponse:
        return FileResponse(USERS_PAGE)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Docker's health check and the CI smoke test; open even when login is on."""
        return {"ok": True}

    @app.get("/api/status")
    async def status() -> dict:
        return snapshot()

    @app.get("/api/downloads")
    async def downloads() -> dict:
        if not auto:
            return {"enabled": False, "active": [], "queue": []}
        return {"enabled": True, **auto.snapshot()}

    async def live_check(s: Streamer) -> str | None:
        """Real channel check; its result also fills the new card right away."""
        st = await mon.monitor_for(s.platform).check(s)
        if st.state is LiveState.UNKNOWN:
            return st.error or "频道检查失败"
        mon.statuses[s] = st
        return None

    verify = check_channel or live_check

    def apply() -> None:
        """Save the roster and hand the new list to everything that is running."""
        roster.save()
        mon.set_streamers(roster.streamers())
        if auto:
            auto.streamers = roster.streamers()
            auto.keep = roster.keep_map(auto.default_keep)
            auto.uploads = roster.upload_map()

    @app.get("/api/streamers")
    async def list_streamers() -> dict:
        return {"streamers": roster.to_dicts()}

    @app.post("/api/streamers", status_code=201)
    async def add_streamer(body: NewStreamer) -> dict:
        before = list(roster.entries)
        try:
            entry = roster.add(body.name, body.platform, body.channel)
        except RosterError as e:
            raise HTTPException(e.status, str(e)) from None
        s = roster.streamers()[-1]
        if err := await verify(s):
            roster.entries = before
            raise HTTPException(422, f"找不到这个频道: {err}")
        apply()
        return {"id": entry_id(entry)}

    @app.patch("/api/streamers")
    async def update_streamer(id: str, body: StreamerSettings) -> dict:
        try:
            entry = roster.update(id, **body.model_dump())
        except RosterError as e:
            raise HTTPException(e.status, str(e)) from None
        apply()
        return {"id": entry_id(entry)}

    @app.delete("/api/streamers")
    async def delete_streamer(id: str) -> dict:
        try:
            roster.remove(id)
        except RosterError as e:
            raise HTTPException(e.status, str(e)) from None
        apply()
        return {"id": id}

    @app.post("/api/refresh")
    async def refresh() -> dict:
        await mon.refresh()
        return snapshot()

    return app


def main() -> None:
    p = argparse.ArgumentParser(description="Live monitor web dashboard.")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--interval", type=float, help="seconds between polls (overrides config)")
    p.add_argument("--dev", action="store_true",
                   help="local preview: pages only, no auto-download and no Discord messages")
    p.add_argument("--reload", action="store_true", help="with --dev: restart when backend code changes")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request log prints the webhook secret
    print(f"Dashboard: http://{args.host}:{args.port}")
    if args.reload:
        # uvicorn's reloader imports the app by name: pass the options through the environment.
        os.environ["KIRI_DEV_CONFIG"] = str(args.config)
        uvicorn.run("backend.monitor.server:dev_app", factory=True, host=args.host, port=args.port,
                    log_level="warning", reload=True, reload_dirs=[str(REPO_ROOT / "backend")])
        return
    uvicorn.run(create_app(args.config, args.interval, dev=args.dev), host=args.host, port=args.port,
                log_level="warning")


def dev_app() -> FastAPI:
    """App factory for `--dev --reload`."""
    return create_app(Path(os.environ.get("KIRI_DEV_CONFIG") or REPO_ROOT / "config.json"), dev=True)


if __name__ == "__main__":
    main()
