"""Web dashboard for the live monitor.

    uv run -m backend.monitor.server            # http://127.0.0.1:8770
    uv run -m backend.monitor.server --port 9000

Polls in the background; the page reads /api/status.
"""

import argparse
import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

from backend.notify import load_notifier

from . import StreamMonitor
from .__main__ import REPO_ROOT, load_config

PAGE = REPO_ROOT / "frontend" / "monitor.html"


def create_app(config: Path, interval: float | None = None) -> FastAPI:
    streamers, cfg_interval = load_config(config)
    interval = interval or cfg_interval
    mon = StreamMonitor(streamers)
    notifier = load_notifier(config)
    if notifier:
        mon.on_change.append(notifier.on_change)
        print("Notify: " + ", ".join(n.name for n in notifier.notifiers))
    else:
        print("Notify: off (set DISCORD_WEBHOOK_URL in .env to enable)")

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(mon.run(interval))
        yield
        task.cancel()
        await mon.aclose()
        if notifier:
            await notifier.aclose()

    app = FastAPI(title="Kiri Live Monitor", lifespan=lifespan)

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

    print(f"Dashboard: http://{args.host}:{args.port}")
    uvicorn.run(create_app(args.config, args.interval), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
