"""The Quark drive: everything Quark, behind "is the drive available", "log in", "account
status" and "upload this archive's drive copy into this folder".

It runs Quark's official CLI (`quarkclouddrive` 1.0.25, a Node script) as a subprocess; read
docs/adr/0001-quark-drive-through-the-official-agent-cli.md first. config.json names the
CLI's script, which sits in the data volume with the state the CLI writes next to it:

    "drive": { "quark_cli": "data/quark/quarkclouddrive-1.0.25/scripts/quark-drive.cjs" }

A missing file (or an empty value) is "drive not available". The CLI prints one JSON object
per line among other lines; only objects with a `type` are read, and the last `result` line
decides. The answers this module relies on were recorded in ticket 01 (2026-11-18).

By hand (on the server: `docker compose exec kiri /app/.venv/bin/python -m backend.quark …`):

    python -m backend.quark login                  # prints the authorization link
    python -m backend.quark login --code <授权码>   # completes it
    python -m backend.quark status                 # authorized? membership, space
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from backend.drive import LOGIN_COMMAND, DriveAuthorizationLapsed, DriveFailed

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLI = "data/quark/quarkclouddrive-1.0.25/scripts/quark-drive.cjs"
NODE = "node"
IDLE_TIMEOUT = 600.0  # seconds without a progress line before a run is killed (the watchdog)

# Result codes that mean "authorize again" (ticket 01): -103 no state (with `action` login),
# 11001 tokens refused after the CLI's own refresh; -106 and -1408 from the CLI's documentation.
LAPSED_CODES = {-103, 11001, -106, -1408}
ALREADY_AUTHORIZED = -118  # `login` while authorized; its message carries the nickname


# ---- running the CLI ----

class CliProcess(Protocol):
    """One run of the CLI: its output lines as they come (ending when it exits), and a way
    to stop it."""

    def __iter__(self) -> Iterator[str]: ...

    def kill(self) -> None: ...


class _Popen:
    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        self._proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", bufsize=1)

    def __iter__(self) -> Iterator[str]:
        assert self._proc.stdout is not None
        try:
            for raw in self._proc.stdout:
                yield raw.rstrip("\r\n")
        finally:
            self._proc.stdout.close()
            self._proc.wait()

    def kill(self) -> None:
        self._proc.kill()


def run_cli(argv: list[str], cwd: Path, env: dict[str, str]) -> CliProcess:
    """Start the CLI (`argv` is the whole command line) in `cwd` with exactly `env`. The one
    function that touches the real CLI: tests replace it."""
    return _Popen(argv, cwd, env)


def _objects(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """The output lines that are JSON objects with a `type`; every other line is ignored."""
    for text in lines:
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict) and "type" in obj:
            yield obj


@dataclass(frozen=True)
class Account:
    """What `status` shows. Never the nickname."""

    membership: str  # e.g. SVIP; "" without one
    ends: datetime | None  # when the membership ends
    used: int  # bytes
    capacity: int  # bytes


class AlreadyAuthorized(Exception):
    """`login` found the drive authorized already."""


class Quark:
    """The drive, reached through the CLI script `cli`."""

    def __init__(self, cli: Path, idle_timeout: float = IDLE_TIMEOUT) -> None:
        self.cli = cli
        self.idle_timeout = idle_timeout

    def _run(self, args: list[str], cwd: Path | None = None,
             on_object: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Run the CLI with `args` and return its last `result` object. Every object read
        goes to `on_object` as it comes. Raises DriveFailed when the run gave no result, or
        was killed after `idle_timeout` seconds without a progress line."""
        # The CLI refuses to run outside an AI agent host it recognises; this marker says it
        # is one. Deliberate and accepted by the owner: see
        # docs/adr/0001-quark-drive-through-the-official-agent-cli.md. Only the subprocess gets it.
        env = {**os.environ, "CLAUDECODE": "1"}
        with tempfile.TemporaryDirectory(prefix="quark-") as scratch:
            process = run_cli([NODE, str(self.cli), *args], cwd or Path(scratch), env)
            last_progress = time.monotonic()
            done, killed = threading.Event(), threading.Event()

            def watchdog() -> None:
                while not done.wait(min(1.0, self.idle_timeout / 4)):
                    if time.monotonic() - last_progress > self.idle_timeout:
                        killed.set()
                        process.kill()
                        return

            threading.Thread(target=watchdog, daemon=True).start()
            last = None
            try:
                for obj in _objects(process):
                    if obj["type"] == "progress":
                        last_progress = time.monotonic()
                    elif obj["type"] == "result":
                        last = obj
                    if on_object:
                        on_object(obj)
            finally:
                done.set()
        if killed.is_set():
            minutes = self.idle_timeout / 60
            raise DriveFailed(f"夸克网盘 CLI {minutes:g} 分钟没有进度，已中止（{args[0]}）")
        if last is None:
            raise DriveFailed(f"夸克网盘 CLI 没有给出结果（{args[0]}）")
        return last

    def _call(self, args: list[str], **kw: Any) -> dict[str, Any]:
        """`_run`, and the result's `data` when its code is 0. Raises DriveAuthorizationLapsed
        or DriveFailed (with the CLI's message) otherwise."""
        res = self._run(args, **kw)
        code = res.get("code")
        if code == 0:
            data = res.get("data")
            return data if isinstance(data, dict) else {}
        msg = str(res.get("msg") or f"错误码 {code}")
        if code in LAPSED_CODES or (res.get("action") == "login" and args[0] != "login"):
            raise DriveAuthorizationLapsed(msg)
        raise DriveFailed(f"{msg}（{code}）")

    # ---- authorizing ----

    def login_link(self) -> str:
        """The page where the owner authorizes this device (it shows the code to paste).
        Raises AlreadyAuthorized, or DriveFailed when the CLI gives no link."""
        res = self._run(["login"])
        if res.get("code") == ALREADY_AUTHORIZED:
            raise AlreadyAuthorized()  # its message names the account: not shown
        # Without a browser to open, the CLI ends with the link inside its message.
        msg = str(res.get("msg") or "")
        link = re.search(r"https?://[^\s\])]+", msg)
        if link is None:
            raise DriveFailed(f"夸克网盘 CLI 没有给出授权链接：{msg}（{res.get('code')}）")
        return link.group(0)

    def login(self, code: str) -> None:
        """Complete the authorization with the code from the authorization page. Raises
        DriveFailed with the CLI's message when it is refused."""
        res = self._run(["login", "--token", code])
        if res.get("code") != 0:
            raise DriveFailed(f"{res.get('msg') or '授权失败'}（{res.get('code')}）")

    # ---- the account ----

    def account(self) -> Account:
        """The account's membership and space. Raises DriveAuthorizationLapsed or DriveFailed."""
        data = self._call(["get-user-info"])
        vip = data.get("vipInfo") or {}
        ends = vip.get("expiresIn")
        return Account(membership=str(vip.get("vipType") or ""),
                       ends=datetime.fromtimestamp(ends / 1000, timezone.utc) if isinstance(ends, int | float) else None,
                       used=int(vip.get("used") or 0), capacity=int(vip.get("capacity") or 0))


def load_quark(config: Path) -> Quark | None:
    """The drive config.json names, or None when it is not available (no CLI file, or the
    feature off): logs one warning then."""
    cfg = json.loads(config.read_text(encoding="utf-8")).get("drive") or {}
    value = cfg.get("quark_cli", DEFAULT_CLI)
    if not value:
        log.warning("drive not available: drive.quark_cli is empty in %s", config)
        return None
    cli = Path(value) if Path(value).is_absolute() else REPO_ROOT / value
    if not cli.is_file():
        log.warning("drive not available: no Quark CLI at %s", cli)
        return None
    return Quark(cli)


# ---- by hand ----

def size(n: int) -> str:
    """Bytes as GB, or TB from 1024 GB."""
    gb = n / 1024**3
    return f"{gb / 1024:.1f} TB" if gb >= 1024 else f"{gb:.1f} GB"


def status_command(drive: Quark) -> int:
    try:
        account = drive.account()
    except DriveAuthorizationLapsed as e:
        print(f"未授权或授权已失效：{e}\n授权：{LOGIN_COMMAND}")
        return 1
    except DriveFailed as e:
        print(f"读取失败：{e}")
        return 1
    ends = f"（到期 {account.ends:%Y-%m-%d}）" if account.ends else ""
    print(f"已授权\n会员：{account.membership or '无'}{ends}\n"
          f"空间：已用 {size(account.used)} / 共 {size(account.capacity)}")
    return 0


def login_command(drive: Quark, code: str | None) -> int:
    try:
        if code:
            drive.login(code)
            print("授权成功")
            return 0
        link = drive.login_link()
    except AlreadyAuthorized:
        print("已授权，不用再授权")
        return 0
    except DriveFailed as e:
        print(f"授权失败：{e}")
        return 1
    command = LOGIN_COMMAND.removesuffix(" login")
    print(f"在浏览器打开这个链接，登录夸克网盘并授权：\n{link}\n"
          f"授权后页面会给出授权码，再运行：\n{command} login --code <授权码>")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Chinese on a Windows console
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="backend.quark", description="The Quark drive: authorize, status, drive copies.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    sub = p.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", parents=[common], help="print the authorization link; with --code, complete it")
    login.add_argument("--code", help="the authorization code (授权码) the authorization page shows")
    sub.add_parser("status", parents=[common], help="whether the drive is authorized, the membership and the space")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    drive = load_quark(args.config)
    if drive is None:
        print("夸克网盘不可用：没有找到 CLI（见 config.json 的 drive.quark_cli）", file=sys.stderr)
        return 2
    if args.command == "login":
        return login_command(drive, args.code)
    return status_command(drive)


if __name__ == "__main__":
    sys.exit(main())
