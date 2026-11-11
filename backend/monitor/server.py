"""Web dashboard for the live monitor.

    uv run -m backend.monitor.server            # http://127.0.0.1:8770
    uv run -m backend.monitor.server --port 9000

Polls in the background; the page reads /api/status.
"""

import argparse
import asyncio
import contextlib
import logging
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


def create_app(config: Path, interval: float | None = None) -> FastAPI:
    streamers, cfg_interval = load_config(config)
    interval = interval or cfg_interval
    mon = StreamMonitor(streamers)
    notifier, auto = attach(config, mon)
    if notifier and notifier.notifiers:
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
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request log prints the webhook secret
    print(f"Dashboard: http://{args.host}:{args.port}")
    uvicorn.run(create_app(args.config, args.interval), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
