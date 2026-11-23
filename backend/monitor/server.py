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
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.archive import Downloads
from backend.auth import auth_enabled, is_admin, load_auth
from backend.downloader import load_settings
from backend.downloader.auto import ShortDownloadLink, attach
from backend.flash.client import flash_configured, load_flash
from backend.group_files import GroupFileKeeper
from backend.group_files.report import GroupFileReport
from backend.notify.incidents import IncidentAlerts
from backend.notify.qq_gateway import QQGateway
from backend.quark import load_quark
from backend.source_record_sheet import SourceRecordKeeper
from backend.state_file import StateFileError, set_incident_hook
from backend.tencent_docs import TencentDocsError, load_credentials

from . import StreamMonitor
from .__main__ import REPO_ROOT, load_config
from .archives import create_router
from .auth_api import install_auth, install_open_me, short_link, visible_streamers
from .notify_api import create_notify_router
from .base import LiveState, Streamer, StreamStatus
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
    drive: bool | None = None  # upload each live's drive copy to Quark once it is processed
    drive_folder: str | None = None  # folder on the drive; "" means the default 录播/<name>
    flash: bool | None = None  # put each live on QQ's flash transfer once it is processed


def has_tencent_docs_token(config: Path) -> bool:
    """The service has a Tencent Docs token (.env), so a source record sheet can be written."""
    try:
        load_credentials(config)
    except TencentDocsError:
        return False
    return True


def create_app(config: Path, interval: float | None = None, dev: bool = False,
               check_channel: ChannelCheck | None = None) -> FastAPI:
    """`dev`: local preview of the pages. Still shows live status, but never downloads,
    transcribes, analyzes or sends Discord messages.
    `check_channel`: replaces the real "does this channel exist" check (tests).
    Login is on when the config has `"auth": {"enabled": true}` (never in dev mode).
    A damaged runtime state file reaches the owner as an operator alert (dev mode: logged)."""
    # First, so incidents while the stores load below are held until the notifier runs
    # (dev mode has no notifier: they are only logged).
    alerts = IncidentAlerts()
    set_incident_hook(alerts)
    _, cfg_interval = load_config(config)
    interval = interval or cfg_interval
    roster = load_roster(config)
    mon = StreamMonitor(roster.streamers())
    # The service's one drive (None without the Quark CLI, and in dev mode): the pipeline and
    # the group-files module upload through it, so their uploads take turns.
    drive = None if dev else load_quark(config)
    # The flash service's one client (None when none is configured, and in dev mode): the
    # pipeline makes flash transfers through it, the 通知 page shows its account.
    flash = None if dev else load_flash(config)
    notifier, auto = (None, None) if dev else attach(config, mon, drive, flash)
    if dev:
        print("Dev mode: no auto-download, no notifications")
    elif notifier and notifier.notifiers:
        print("Notify: " + ", ".join(n.name for n in notifier.notifiers))
    else:
        print("Notify: off (set DISCORD_WEBHOOK_URL in .env to enable)")
    print(f"Auto-download: {len(auto.keep)} streamer(s)" if auto else "Auto-download: off")
    # Group files to the drive: the gateway hands them over. The drive is the service's one
    # (None: files are ignored, with a warning); its state files sit beside the QQ groups
    # file, local copies on the downloads volume; failed files are retried with the pipeline's
    # interval and maximum of errors. The group is told through the QQ bot, the owner
    # through the operator alert.
    group_files = GroupFileKeeper(
        notifier.qq.groups, drive,
        temp=Downloads(load_settings(config)[0].output_dir).group_files,
        report=GroupFileReport(notifier.qq.api, notifier.qq.groups, alert=notifier.alert, history=notifier.history),
        **({"retry_interval": auto.retry_interval, "max_errors": auto.max_errors} if auto else {}),
    ) if notifier and notifier.qq else None
    # The QQ bot's event connection: keeps it online and learns which groups it is in.
    gateway = QQGateway(
        notifier.qq.settings, notifier.qq.groups, alert=notifier.alert,
        names=lambda: {entry_id(e): e.get("name") or e["channel"] for e in roster.entries},
        take_group_file=group_files.posted if group_files else None,
    ) if notifier and notifier.qq else None
    # The source record sheets: with QQ (the groups say whose lives go to which sheet) and the
    # Tencent Docs token, the pipeline tells the module each processed live; else nothing. Its
    # state files (the rows waiting, the rows written) sit beside the QQ groups file. A row that
    # cannot be written is tried again with the pipeline's interval and maximum of errors; the
    # owner is told of one given up on through the operator alert.
    source_record = (SourceRecordKeeper(
        load_credentials(config), groups=notifier.qq.groups, alert=notifier.alert,
        **({"retry_interval": auto.retry_interval, "max_errors": auto.max_errors} if auto else {}),
    ) if notifier and notifier.qq and has_tencent_docs_token(config) else None)
    if auto:
        auto.source_record = source_record

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        alerts.start(notifier)
        task = asyncio.create_task(mon.run(interval))
        auto_task = asyncio.create_task(auto.run()) if auto else None
        qq_task = asyncio.create_task(gateway.run()) if gateway else None
        files_task = asyncio.create_task(group_files.run()) if group_files else None
        records_task = asyncio.create_task(source_record.run()) if source_record else None
        yield
        task.cancel()
        if auto_task:
            auto_task.cancel()
        if qq_task:
            qq_task.cancel()
            await gateway.aclose()
        if files_task:
            files_task.cancel()
            await asyncio.gather(files_task, return_exceptions=True)
            await group_files.aclose()
        if records_task:
            records_task.cancel()
            await asyncio.gather(records_task, return_exceptions=True)
            source_record.close()
        await mon.aclose()
        alerts.stop()
        if notifier:
            await notifier.aclose()

    app = FastAPI(title="Kiri Live Monitor", lifespan=lifespan)
    app.state.notifier = notifier
    app.state.auto = auto
    app.state.drive = drive
    app.state.gateway = gateway
    app.state.group_files = group_files
    app.state.source_record = source_record
    def streamer_names() -> set[str]:
        return {e.get("name") or e["channel"] for e in roster.entries}

    def visible(request: Request) -> set[str] | None:
        """Streamer names this request may see; None = everyone (see auth_api.visible_streamers)."""
        return visible_streamers(request, streamer_names())

    auth = load_auth(config) if auth_enabled(config) and not dev else None
    if auth:
        install_auth(app, auth, streamer_names)
        if auto:  # the 全部下载 address of a message downloads without logging in
            def short_download_link(folder: str) -> ShortDownloadLink:
                code = auth.download_links.create(folder)
                return ShortDownloadLink(short_link(code), auth.download_links.expires(code))

            auto.download_link = short_download_link
        print("Login: on")
    else:
        install_open_me(app)
        print("Login: off")
    app.include_router(create_router(load_settings(config)[0].output_dir, visible,
                                     auth.download_links.folder if auth else None))
    app.include_router(create_notify_router(notifier, roster, gateway, drive_available=drive is not None,
                                            tencent_docs_available=has_tencent_docs_token(config), flash=flash,
                                            flash_waiting=auto.flash_waiting if auto else None))

    def snapshot(names: set[str] | None = None) -> dict:
        rows = []
        for s in mon.streamers:
            if names is not None and s.name not in names:
                continue
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
    async def status(request: Request) -> dict:
        return snapshot(visible(request))

    @app.get("/api/downloads")
    async def downloads(request: Request) -> dict:
        # Group files have no streamer and name a QQ group's files: only for the owner (an
        # admin, or login off), under their own key, and only while there are some.
        user = getattr(request.state, "user", None)
        files = group_files.snapshot() if group_files and (user is None or is_admin(user)) else None
        extra = {"group_files": files} if files and (files["active"] or files["queue"]) else {}
        if not auto:
            return {"enabled": False, "active": [], "queue": [], **extra}
        snap, names = auto.snapshot(), visible(request)
        if names is not None:
            snap = {**snap, **{k: [j for j in snap.get(k) or [] if j.get("name") in names]
                               for k in ("active", "queue")}}
        return {"enabled": True, **snap, **extra}

    checked: dict[Streamer, StreamStatus] = {}  # a new streamer's check, until she is in the roster

    async def live_check(s: Streamer) -> str | None:
        """Real channel check; its result is the new streamer's first status (`add_streamer`)."""
        st = await mon.monitor_for(s.platform).check(s)
        if st.state is LiveState.UNKNOWN:
            return st.error or "频道检查失败"
        checked[s] = st
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
            auto.drive_folders = roster.drive_map()
            auto.flash_streamers = roster.flash_streamers()

    @app.get("/api/streamers")
    async def list_streamers(request: Request) -> dict:
        names = visible(request)
        return {"streamers": [s for s in roster.to_dicts() if names is None or s["name"] in names],
                "flash_available": flash_configured()}

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
            checked.pop(s, None)
            raise HTTPException(422, f"找不到这个频道: {err}")
        apply()
        if st := checked.pop(s, None):
            # Fills the new card right away, and tells the notifier: a streamer added
            # while live is announced, and so is the end of that live.
            await mon.observe(st)
        return {"id": entry_id(entry)}

    @app.patch("/api/streamers")
    async def update_streamer(id: str, body: StreamerSettings) -> dict:
        try:
            entry = roster.update(id, flash_available=flash_configured(), **body.model_dump())
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
    async def refresh(request: Request) -> dict:
        await mon.refresh()
        return snapshot(visible(request))

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
    uvicorn.run(app_or_exit(args.config, args.interval, dev=args.dev), host=args.host, port=args.port,
                log_level="warning")


def app_or_exit(config: Path, interval: float | None = None, dev: bool = False) -> FastAPI:
    """create_app, but damaged operator data ends the process with one readable line."""
    try:
        return create_app(config, interval, dev=dev)
    except StateFileError as e:
        print(e.message, file=sys.stderr)
        sys.exit(1)


def dev_app() -> FastAPI:
    """App factory for `--dev --reload`."""
    return app_or_exit(Path(os.environ.get("KIRI_DEV_CONFIG") or REPO_ROOT / "config.json"), dev=True)


if __name__ == "__main__":
    main()
