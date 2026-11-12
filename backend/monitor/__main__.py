"""Run the live monitor.

    uv run -m backend.monitor            # poll forever, print state changes
    uv run -m backend.monitor --once     # check once, print table, exit
    uv run -m backend.monitor --once --json
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import LiveState, StreamMonitor, Streamer, StreamStatus
from .roster import load_roster

REPO_ROOT = Path(__file__).resolve().parents[2]

ICON = {
    LiveState.LIVE: "🔴 LIVE",
    LiveState.UPCOMING: "🕒 UPCOMING",
    LiveState.OFFLINE: "⚫ offline",
    LiveState.UNKNOWN: "❔ unknown",
}


def load_config(path: Path) -> tuple[list[Streamer], float]:
    cfg = json.loads(path.read_text(encoding="utf-8")).get("monitor", {})
    return load_roster(path).streamers(), float(cfg.get("interval", 60))


def fmt(st: StreamStatus) -> str:
    line = f"{ICON[st.state]:<12} [{st.streamer.platform}] {st.streamer.name}"
    if st.title:
        line += f" — {st.title}"
    if st.url:
        line += f"  {st.url}"
    if st.error:
        line += f"  ({st.error})"
    return line


async def main() -> int:
    p = argparse.ArgumentParser(description="Monitor whether streamers are live.")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--json", action="store_true", help="with --once: print JSON")
    p.add_argument("--interval", type=float, help="seconds between polls (overrides config)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request log prints the webhook secret
    streamers, interval = load_config(args.config)
    if not streamers:
        print(f"No streamers under monitor.streamers in {args.config}", file=sys.stderr)
        return 1

    async with StreamMonitor(streamers) as mon:
        if args.once:
            results = await mon.poll_once()
            if args.json:
                print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
            else:
                for r in results:
                    print(fmt(r))
            return 0

        def on_change(new: StreamStatus, old: StreamStatus | None) -> None:
            ts = new.checked_at.astimezone().strftime("%H:%M:%S")
            print(f"{ts}  {fmt(new)}", flush=True)

        from backend.downloader.auto import attach  # lazy: imports this package

        mon.on_change.append(on_change)
        notifier, auto = attach(args.config, mon)
        names = ", ".join(n.name for n in notifier.notifiers) if notifier else ""
        print(f"Monitoring {len(streamers)} streamer(s) every {args.interval or interval:g}s. "
              f"Notify: {names or 'off'}. Auto-download: {'on' if auto else 'off'}. Ctrl+C to stop.")
        auto_task = asyncio.create_task(auto.run()) if auto else None
        try:
            await mon.run(args.interval or interval)
        finally:
            if auto_task:
                auto_task.cancel()
            if notifier:
                await notifier.aclose()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
