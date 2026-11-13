"""Download a VOD (a TwitCasting replay: its replay address) and its chat replay.

    uv run -m backend.downloader <url>             # video + chat
    uv run -m backend.downloader <url> --no-chat   # video only
    uv run -m backend.downloader <url> --no-video  # chat only
    uv run -m backend.downloader <url> --info      # print metadata, download nothing
    uv run -m backend.downloader <url> --wait      # then keep retrying a not-ready chat

    uv run -m backend.downloader --pending         # list the chat retry queue
    uv run -m backend.downloader --retry           # retry every queued chat now
    uv run -m backend.downloader --wait            # retry queued chats on schedule until done
    uv run -m backend.downloader --clean <raw chat file>...   # (re)write .chat_parsed.json
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import REPO_ROOT, DownloadError, PendingChats, download, downloader_for, load_settings
from backend.state_file import print_incidents

from .chat_clean import clean_chat


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def show_pending(queue: PendingChats) -> None:
    entries = queue.load()
    if not entries:
        print("Chat retry queue is empty.")
    for e in entries:
        print(f"• {e.info.creator} — {e.info.title}\n  {e.info.url}\n"
              f"  queued {fmt_time(e.queued_at)}, attempts {e.attempts}, "
              f"next {fmt_time(e.next_try)}, last error: {e.last_error}")


def retry_once(queue: PendingChats, streamers, settings, force: bool) -> None:
    r = queue.retry(streamers, settings, force=force)
    for e, path in r.done:
        print(f"✓ chat: {e.info.title}\n  {path}")
    for e in r.waiting:
        print(f"… still not ready ({e.attempts} tries): {e.info.title}, next {fmt_time(e.next_try)}")
    for e in r.given_up:
        print(f"✗ gave up: {e.info.title} — {e.last_error}")


def wait_loop(queue: PendingChats, streamers, settings) -> None:
    """Retry on schedule until the queue is empty."""
    while (due := queue.next_due()) is not None:
        delay = due - time.time()
        if delay > 0:
            print(f"Waiting until {fmt_time(due)} for the next chat retry (Ctrl+C to stop; "
                  f"the queue is kept)...")
            time.sleep(delay)
        retry_once(queue, streamers, settings, force=False)
    print("Chat retry queue is empty.")


def main() -> int:
    p = argparse.ArgumentParser(description="Download a YouTube / Twitch VOD or a TwitCasting replay, and its chat.")
    p.add_argument("urls", nargs="*")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--no-chat", action="store_true")
    p.add_argument("--info", action="store_true", help="print metadata only")
    p.add_argument("-o", "--output-dir", type=Path, help="overrides download.output_dir")
    p.add_argument("--pending", action="store_true", help="list the chat retry queue")
    p.add_argument("--retry", action="store_true", help="retry every queued chat now")
    p.add_argument("--wait", action="store_true", help="keep retrying queued chats until done")
    p.add_argument("--clean", nargs="+", type=Path, metavar="FILE",
                   help="clean already-downloaded .live_chat.json / .chat.json files")
    args = p.parse_args()
    print_incidents()  # a damaged chat retry queue: tell the owner at the terminal
    if args.clean:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
        bad = 0
        for raw in args.clean:
            try:
                print(f"✓ {clean_chat(raw)}")
            except (OSError, ValueError) as e:
                print(f"✗ {raw}: {e}", file=sys.stderr)
                bad += 1
        return 1 if bad else 0
    if not (args.urls or args.pending or args.retry or args.wait):
        p.error("give a URL, or --pending / --retry / --wait / --clean")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings, streamers = load_settings(args.config)
    if args.output_dir:
        settings.output_dir = args.output_dir.resolve()
    queue = PendingChats.from_settings(settings)

    failed = 0
    for url in args.urls:
        try:
            if args.info:
                info = downloader_for(url, settings, streamers).info(url)
                print(json.dumps(asdict(info), ensure_ascii=False, indent=2))
                continue
            r = download(url, settings, streamers, video=not args.no_video, chat=not args.no_chat)
        except DownloadError as e:
            print(f"✗ {url}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"{'✓' if r.ok else '✗'} {r.info.creator} — {r.info.title}")
        for kind, path in (("video", r.video), ("chat", r.chat)):
            if path:
                print(f"  {kind}: {path}")
            elif kind == "chat" and r.chat_queued:
                print(f"  chat: not ready yet — queued, retried every "
                      f"{settings.chat_retry_interval / 60:.0f} min (--wait to wait for it)")
            elif kind in r.errors:
                print(f"  {kind}: FAILED — {r.errors[kind]}")
        failed += any(k != "chat" or not r.chat_queued for k in r.errors)

    if queue:
        if args.pending:
            show_pending(queue)
        if args.retry:
            retry_once(queue, streamers, settings, force=True)
        if args.wait:
            try:
                wait_loop(queue, streamers, settings)
            except KeyboardInterrupt:
                print("\nStopped; queued chats stay queued.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
