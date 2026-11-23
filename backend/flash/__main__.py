"""The flash module by hand (on the server:
`docker compose exec kiri /app/.venv/bin/python -m backend.flash …`):

    python -m backend.flash status     # whether the flash account is online, in one line

The settings come from `.env` (see backend/flash/client.py).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from pathlib import Path

from .client import ACTIONS_URL_ENV, REPO_ROOT, AccountState, FlashClient, FlashFailed, load_flash


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


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """`env`: the settings, for tests; the process environment with `.env` by default."""
    for stream in (sys.stdout, sys.stderr):  # Chinese on a Windows console
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="backend.flash", description="Flash transfers: the flash account's state.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common], help="whether the flash account is online")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    client = load_flash(args.config, env)
    if client is None:
        print(f"没有设置闪传服务（.env 里的 {ACTIONS_URL_ENV}）", file=sys.stderr)
        return 2
    return status_command(client)


if __name__ == "__main__":
    sys.exit(main())
