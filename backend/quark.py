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
    python -m backend.quark upload <archive folder or a folder of them> [--folder 录播/某主播]

A drive copy goes into its folder from the top of the drive (made when missing), unless a
zip of that name and size is there already. The upload has no time limit, but it is killed
and failed after IDLE_TIMEOUT without a progress line; a failed upload's task record is
deleted, and `upload resume` is never used (it sends the whole file again).
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

from backend.archive import Archive, ArtifactDamaged, ArtifactMissing, Artifact, Downloads, DriveLink, safe_name
from backend.downloader import load_settings
from backend.monitor.roster import drive_folder, drive_folder_error, load_roster
from backend.drive import LOGIN_COMMAND, DriveAuthorizationLapsed, DriveCopy, DriveFailed, DriveProgress
from backend.drive_copy import drive_copy

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


@dataclass(frozen=True)
class DriveUpload:
    """What became of one archive's drive copy."""

    copy: DriveCopy
    sent: bool  # False: a zip of that name and size was on the drive already


class Quark:
    """The drive, reached through the CLI script `cli`. Drive copies are built under the
    downloads root `downloads` (see backend.drive_copy). A Quark is the pipeline's `Drive`."""

    def __init__(self, cli: Path, downloads: Path, idle_timeout: float = IDLE_TIMEOUT) -> None:
        self.cli = cli
        self.downloads = downloads
        self.idle_timeout = idle_timeout

    def __call__(self, archive: Archive, folder: str, progress: DriveProgress) -> DriveCopy:
        return self.upload(archive, folder, progress).copy

    # ---- drive copies ----

    def upload(self, archive: Archive, folder: str, progress: DriveProgress | None = None) -> DriveUpload:
        """Upload the archive's drive copy into `folder` (a path from the top of the drive,
        made when missing), unless a zip of that name and size is there already, and make its
        drive link (saved in the archive). An archive whose drive link names that zip, and
        whose zip is there, is left alone. Progress goes to `progress(sent, total)`. Raises
        DriveAuthorizationLapsed, or DriveFailed with the CLI's message (also for an archive
        without video: nothing is uploaded). A share that fails is not a failed upload: the
        drive copy comes back without a link, with the reason."""
        try:
            kept = archive.read_drive_link()
        except (ArtifactMissing, ArtifactDamaged, OSError):
            kept = None
        with drive_copy(archive, self.downloads) as zip_path:
            if zip_path is None:
                raise DriveFailed("没有影片，没有可上传的网盘副本")
            path = f"{folder}/{zip_path.name}"
            size = zip_path.stat().st_size
            # The path from the top of the drive (`--parent-fid 0`); the same folder when it exists.
            folder_id = str(self._call(["create-folder", "--dir-path", folder, "--parent-fid", "0"]).get("fid") or "")
            if not folder_id:
                raise DriveFailed(f"夸克网盘 CLI 没有给出文件夹 {folder} 的 id")
            there = next((f for f in self._children(folder_id)
                          if f.get("filename") == zip_path.name and f.get("size") == size), None)
            if there is not None:
                log.info("drive copy already on the drive: %s", path)
                if kept is not None and kept.path == path:
                    return DriveUpload(DriveCopy(path, kept.url), sent=False)
                # A listing's id differs in every listing: good for a share made right now only.
                return DriveUpload(self._share(archive, str(there.get("fid") or ""), path), sent=False)
            file_id = self._send(zip_path, folder_id, progress)
            log.info("drive copy uploaded: %s", path)
            return DriveUpload(self._share(archive, file_id, path), sent=True)

    def _share(self, archive: Archive, file_id: str, path: str) -> DriveCopy:
        """Share the zip at `path` (drive id `file_id`) as a private link that never expires,
        and note the drive link in the archive. Raises DriveAuthorizationLapsed only: a share
        that fails otherwise gives the drive copy without a link, with the reason."""
        if not file_id:
            return DriveCopy(path, None, "夸克网盘 CLI 没有给出文件的 id，没法分享")
        try:
            # --url-type 2: private, Quark makes the extraction code; --expired-type 1: never expires.
            data = self._call(["share", file_id, "--url-type", "2", "--expired-type", "1"])
        except DriveFailed as e:
            log.warning("drive copy uploaded but not shared: %s: %s", path, e)
            return DriveCopy(path, None, str(e))
        url, code = str(data.get("share_url") or ""), str(data.get("passcode") or "")
        if not url or not code:
            return DriveCopy(path, None, "夸克网盘 CLI 没有给出分享地址或提取码")
        link = DriveLink(f"{url}{'&' if '?' in url else '?'}pwd={code}", code, path, datetime.now(timezone.utc))
        try:
            archive.save(Artifact.DRIVE_LINK, link)
        except (OSError, ValueError) as e:
            log.warning("drive link of %s not noted in the archive: %s", path, e)
        log.info("drive link made: %s", path)
        return DriveCopy(path, link.url)

    def _children(self, folder_id: str) -> list[dict[str, Any]]:
        """Every file and folder directly in the folder: `browse --all` writes them to a JSONL
        file in the working directory and names it in its `artifact` line."""
        with tempfile.TemporaryDirectory(prefix="quark-browse-") as scratch:
            found: list[str] = []

            def note(obj: dict[str, Any]) -> None:
                if obj["type"] == "artifact" and isinstance(obj.get("data"), dict):
                    found.append(str(obj["data"].get("file_path") or ""))

            self._call(["browse", "--parent-fid", folder_id, "--all"], cwd=Path(scratch), on_object=note)
            if not found or not found[-1]:
                raise DriveFailed("夸克网盘 CLI 没有给出文件夹的完整列表")
            listing = Path(scratch) / found[-1]  # absolute as the CLI gives it; else in its working directory
            children = []
            for text in listing.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(text)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    children.append(entry)
            return children

    def _send(self, zip_path: Path, folder_id: str, progress: DriveProgress | None) -> str:
        """`upload`, and the uploaded file's id ("" when the CLI gave none). After a failed
        upload its task record is deleted (`upload resume` is never used: it sends the whole
        file again)."""
        records: list[str] = []
        file_ids: list[str] = []

        def note(obj: dict[str, Any]) -> None:
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            if obj["type"] == "progress" and progress:
                total = data.get("total")
                progress(int(data.get("current") or 0), int(total) if isinstance(total, int | float) else None)
            elif obj["type"] == "list" and data.get("recordId"):
                records.append(str(data["recordId"]))
                if data.get("fileId"):
                    file_ids.append(str(data["fileId"]))

        try:
            data = self._call(["upload", str(zip_path), "--parent-fid", folder_id], on_object=note)
        except (DriveFailed, DriveAuthorizationLapsed):
            self._forget_upload(zip_path.name, records)
            raise
        fids = data.get("fids")
        return file_ids[-1] if file_ids else str(fids[0]) if isinstance(fids, list) and fids else ""

    def _forget_upload(self, name: str, records: list[str]) -> None:
        """Delete the task records of a failed upload of `name`: the ones it named, else (it
        was killed before naming one) every record of that name not finished."""
        try:
            if not records:
                def note(obj: dict[str, Any]) -> None:
                    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
                    if obj["type"] == "list" and data.get("fileName") == name and data.get("state") != "success":
                        records.append(str(data.get("recordId") or ""))

                self._call(["upload", "list"], on_object=note)
            for record in filter(None, records):
                self._call(["upload", "delete", "--record-id", record])
        except (DriveFailed, DriveAuthorizationLapsed) as e:
            log.warning("could not delete the task record of a failed upload of %s: %s", name, e)

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
    log.info("drive available: Quark CLI at %s", cli)
    return Quark(cli, load_settings(config)[0].output_dir)


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


def archive_folders(path: Path) -> list[Path]:
    """`path` when it is an archive folder (it holds an artifact), else the archive folders
    below it, in name order. Dot-folders (the drive copies being built) are not looked into."""
    try:
        if Archive.from_folder(path).present():
            return [path]
        subfolders = sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    return [a for p in subfolders for a in archive_folders(p)]


def folder_for(creator: str, entries: list[dict[str, Any]]) -> str:
    """The drive folder of the roster entry named like `creator` (one with a folder written
    in first), else `录播/<creator>`."""
    named = [e for e in entries if safe_name(e.get("name") or e.get("channel") or "") == creator]
    named.sort(key=lambda e: not e.get("drive_folder"))
    return drive_folder(named[0]) if named else f"录播/{creator}"


def upload_command(drive: Quark, config: Path, path: Path, folder: str | None) -> int:
    entries = load_roster(config).entries
    failed = False
    for archive_folder in archive_folders(path):
        archive = Archive.from_folder(archive_folder)
        if archive.downloading:
            try:
                name = Downloads(drive.downloads).ref(archive_folder)
            except ValueError:
                name = str(archive_folder)
            print(f"跳过：{name}（{'没有影片' if archive.video is None else '还在下载'}）")
            continue
        zip_name = f"{archive.stem}.zip"
        try:
            done = drive.upload(archive, folder or folder_for(archive.creator, entries), _print_progress)
        except DriveAuthorizationLapsed as e:
            print(f"失败：{zip_name}：{e}")
            print(f"网盘授权失效，先重新授权：{LOGIN_COMMAND}")
            return 1
        except DriveFailed as e:
            print(f"失败：{zip_name}：{e}")
            failed = True
            continue
        print(f"{'已上传' if done.sent else '已在网盘'}：{zip_name} → {done.copy.path}")
        print(f"  网盘链接：{done.copy.link}" if done.copy.link else f"  没有网盘链接：{done.copy.reason}")
    return 1 if failed else 0


def _print_progress(sent: int, total: int | None) -> None:
    """The upload's progress on stderr, on one line (stdout keeps one line per archive)."""
    if total:
        print(f"\r  {sent * 100 // total}%  {size(sent)} / {size(total)}", end="", file=sys.stderr, flush=True)
        if sent >= total:
            print(file=sys.stderr)


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
    upload = sub.add_parser("upload", parents=[common], help="upload the drive copy of an archive, or of every archive in a folder")
    upload.add_argument("path", type=Path, help="an archive folder, or a folder of them")
    upload.add_argument("--folder", help="the drive folder (default: the streamer's, from the roster; else 录播/<creator>)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    drive = load_quark(args.config)
    if drive is None:
        print("夸克网盘不可用：没有找到 CLI（见 config.json 的 drive.quark_cli）", file=sys.stderr)
        return 2
    if args.command == "login":
        return login_command(drive, args.code)
    if args.command == "upload":
        folder = args.folder.replace("\\", "/").strip(" /") if args.folder else None
        if folder and (err := drive_folder_error(folder)):
            print(err, file=sys.stderr)
            return 2
        return upload_command(drive, args.config, args.path, folder)
    return status_command(drive)


if __name__ == "__main__":
    sys.exit(main())
