"""Send a test notification to every configured channel.

    uv run -m backend.notify            # sample message
    uv run -m backend.notify --offline  # sample go-live + ended messages
    uv run -m backend.notify --live     # announce whoever in config.json is live now
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.monitor import LiveState, StreamMonitor, Streamer, StreamStatus
from backend.monitor.__main__ import load_config
from backend.state_file import print_incidents

from . import REPO_ROOT, EventKind, StreamEvent, load_notifier


async def main() -> int:
    p = argparse.ArgumentParser(description="Send a test go-live notification.")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    p.add_argument("--live", action="store_true", help="send for streamers live right now")
    p.add_argument("--offline", action="store_true", help="also send a sample ended message")
    args = p.parse_args()
    print_incidents()  # damaged notification state: tell the owner at the terminal
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request log prints the webhook secret

    notifier = load_notifier(args.config)
    if not notifier:
        print("No notify channel configured. Set DISCORD_WEBHOOK_URL in .env.", file=sys.stderr)
        return 1

    try:
        if args.live:
            streamers, _ = load_config(args.config)
            async with StreamMonitor(streamers) as mon:
                live = [s for s in await mon.poll_once() if s.is_live]
            if not live:
                print("Nobody in config.json is live right now.")
            for status in live:
                await notifier.notify(StreamEvent(EventKind.LIVE, status))
        else:
            sample = StreamStatus(
                Streamer(name="测试主播", platform="youtube", channel="@test"),
                LiveState.LIVE,
                title="这是一条测试通知（切片小助手 直播通知）",
                url="https://www.youtube.com/",
                started_at=datetime.now(timezone.utc) - timedelta(hours=1, minutes=23),
            )
            await notifier.notify(StreamEvent(EventKind.LIVE, sample))
            if args.offline:
                await notifier.notify(StreamEvent(EventKind.OFFLINE, sample, datetime.now(timezone.utc)))
    finally:
        await notifier.aclose()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
