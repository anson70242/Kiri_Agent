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
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

from backend.downloader import load_settings
from backend.downloader.auto import attach

from . import StreamMonitor
from .__main__ import REPO_ROOT, load_config
from .archives import create_router

PAGE = REPO_ROOT / "frontend" / "monitor.html"


def create_app(config: Path, interval: float | None = None, dev: bool = False) -> FastAPI:
    """`dev`: local preview of the pages. Still shows live status, but never downloads,
    transcribes, analyzes or sends Discord messages."""
    streamers, cfg_interval = load_config(config)
    interval = interval or cfg_interval
    mon = StreamMonitor(streamers)
    notifier, auto = (None, None) if dev else attach(config, mon)
    if dev:
        print("Dev mode: no auto-download, no notifications")
    elif notifier and notifier.notifiers:
        print("Notify: " + ", ".join(n.name for n in notifier.notifiers))
    else:
        print("Notify: off (set DISCORD_WEBHOOK_URL in .env to enable)")
    print(f"Auto-download: {len(auto.keep)} streamer(s)" if auto else "Auto-download: off")

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(mon.run(interval))
        auto_task = asyncio.create_task(auto.run()) if auto else None
        yield
        task.cancel()
        if auto_task:
            auto_task.cancel()
        await mon.aclose()
        if notifier:
            await notifier.aclose()

    app = FastAPI(title="Kiri Live Monitor", lifespan=lifespan)
    app.state.notifier = notifier
    app.include_router(create_router(load_settings(config)[0].output_dir))

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

    @app.get("/api/status")
    async def status() -> dict:
        return snapshot()

    @app.get("/api/downloads")
    async def downloads() -> dict:
        if not auto:
            return {"enabled": False, "active": [], "queue": []}
        return {"enabled": True, **auto.snapshot()}

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
