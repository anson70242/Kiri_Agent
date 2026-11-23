"""The flash module by hand (on the server:
`docker compose exec kiri /app/.venv/bin/python -m backend.flash …`):

    python -m backend.flash status                    # whether the flash account is online, in one line
    python -m backend.flash upload <archive folder>   # that archive's flash transfer; prints its flash link

`upload` takes the archive's folder, or its folder reference under the downloads folder
(`森野鈴/20261122/标题`). It prepares the archive's files (cutting a video too big for one
file), starts the flash transfer, waits for it, notes the flash link in the archive (its page
shows 「闪传链接」) and prints it. It tells no group and writes no sheet row.

The settings come from `.env` (see backend/flash/client.py).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Mapping
from datetime import timedelta, timezone
from pathlib import Path

from backend.archive import Archive

from .client import (ACTIONS_URL_ENV, REPO_ROOT, STALL_SECONDS, AccountState, FlashClient, FlashFailed,
                     FlashUnavailable, Progress, load_flash)
from .files import needs_cutting, note_flash_link, prepare, remove_staging

POLL_SECONDS = 10  # between two looks at an upload's progress
HONG_KONG = timezone(timedelta(hours=8))


def describe(state: AccountState) -> str:
    """The account's state in one line, without the QR code's text."""
    if state.state == "online":
        return "闪传账号：在线"
    if state.state == "unreachable":
        return "闪传账号：连不上闪传服务"
    parts = ["闪传账号：掉线"]
    if state.error:
        parts.append(f"闪传服务说：{state.error}")
    if state.qr_code:
        parts.append("到网页的「通知」页扫二维码登录")
    if state.missing:
        parts.append(state.missing)
    return "，".join(parts)


def status_command(client: FlashClient) -> int:
    try:
        state = client.account()
    except FlashFailed as e:
        print(f"读取失败：{e}")
        return 1
    print(describe(state))
    return 0 if state.state == "online" else 1


def upload_command(client: FlashClient, folder: str, ffmpeg: str | None, *,
                   sleep: Callable[[float], None], clock: Callable[[], float]) -> int:
    """One archive's flash transfer, waited for; 0 with the flash link printed, 1 when it
    could not be made (the staging folder is removed either way)."""
    path = Path(folder)
    if not path.is_dir():
        path = client.downloads / folder
    try:
        archive = Archive.from_folder(path)
    except OSError:
        archive = None
    if archive is None or not archive.present():
        print(f"不是录播文件夹：{folder}")
        return 1
    try:
        if needs_cutting(archive):
            print("影片超过单个文件的上限，先切成几段……")
        prepared = prepare(archive, client.downloads, ffmpeg=ffmpeg)
        print(f"闪传「{prepared.name}」：{len(prepared.files)} 个文件")
        started = client.start(prepared.files, prepared.name)
        _wait(client, started.id, sleep, clock)
        note_flash_link(archive, started)
    except FlashUnavailable as e:
        print(f"闪传账号掉线或连不上闪传服务：{e}")
        return 1
    except FlashFailed as e:
        print(f"闪传失败：{e}")
        return 1
    finally:
        remove_staging(archive, client.downloads)
    expires = started.expires.astimezone(HONG_KONG)
    print(f"闪传链接（有效至 {expires.year}/{expires.month}/{expires.day} {expires:%H:%M}，香港时间）：{started.link}")
    return 0


def _wait(client: FlashClient, transfer_id: str, sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
    """Asks for the flash transfer's progress until it is finished, printing it now and then.
    FlashFailed when a file fails or the bytes sent do not move for STALL_SECONDS."""
    sent, moved_at, shown = -1, clock(), -1
    while True:
        progress = client.progress(transfer_id)
        if progress.failed:
            raise FlashFailed(f"{progress.failed} 个文件上传失败")
        if progress.finished:
            print(f"上传完成：{progress.done} 个文件")
            return
        if progress.sent != sent:
            sent, moved_at = progress.sent, clock()
        elif clock() - moved_at >= STALL_SECONDS:
            raise FlashFailed(f"上传 {STALL_SECONDS // 60} 分钟没有进展")
        if (tenth := _percent(progress) // 10) != shown:
            shown = tenth
            print(f"上传中：{_percent(progress)}%（{progress.sent / 1e9:.2f} / {progress.total / 1e9:.2f} GB，"
                  f"{progress.done} 个文件已传完）")
        sleep(POLL_SECONDS)


def _percent(progress: Progress) -> int:
    return progress.sent * 100 // progress.total if progress.total else 0


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None, *,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    """`env`: the settings, for tests; the process environment with `.env` by default.
    `sleep` and `clock`: the waiting of `upload`, for tests."""
    for stream in (sys.stdout, sys.stderr):  # Chinese on a Windows console
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="backend.flash", description="Flash transfers by hand.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common], help="whether the flash account is online")
    upload = sub.add_parser("upload", parents=[common], help="one archive's flash transfer; prints its flash link")
    upload.add_argument("folder", help="the archive's folder, or its folder reference under the downloads folder")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    client = load_flash(args.config, env)
    if client is None:
        print(f"没有设置闪传服务（.env 里的 {ACTIONS_URL_ENV}）", file=sys.stderr)
        return 2
    if args.command == "upload":
        from backend.downloader import load_settings

        return upload_command(client, args.folder, load_settings(args.config)[0].ffmpeg, sleep=sleep, clock=clock)
    return status_command(client)


if __name__ == "__main__":
    sys.exit(main())
