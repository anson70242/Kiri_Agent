"""Deploy check: would this code start on the state files that are here, and is the
download queue idle?

    uv run -m backend.check_state [--config config.json] [--require-idle]

It builds the stores the way the service does, inside a dry run (backend.state_file), so
every state file is read with the decoder the service would use and nothing on disk
changes: no backup, no seeded roster, no alert. The deploy workflow runs it in the new
image against the server's data before anything restarts.

Exit code: 0 fine, 1 a state file is damaged, 2 the download queue still holds jobs
(only with --require-idle).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.auth import load_auth
from backend.downloader import load_settings
from backend.downloader.auto import load_auto
from backend.downloader.pending import PendingChats
from backend.monitor.roster import load_roster
from backend.notify import load_notifier
from backend.state_file import Damage, dry_run

REPO_ROOT = Path(__file__).resolve().parents[1]

# What the service would do with a damaged file, by its policy.
OUTCOME = {"strict": "新版本会拒绝启动", "recover": "新版本会把它备份后清空"}


def check(config: Path) -> tuple[list[Damage], int]:
    """The damaged state files among those the service would load with this config, and
    how many jobs wait in the download queue."""
    with dry_run() as damaged:
        load_roster(config)  # the roster
        load_auth(config)  # users, sessions
        load_notifier(config, allow_empty=True)  # live-stream state, notification history, QQ groups
        auto = load_auto(config)
        jobs = auto.load() if auto else []  # the download queue
        settings, _ = load_settings(config)
        if pending := PendingChats.from_settings(settings):
            pending.load()  # the chat retry queue
    unique = {d.path: d for d in damaged}  # the roster is loaded twice (load_auto reads it too)
    return list(unique.values()), len(jobs)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="backend.check_state", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    p.add_argument("--require-idle", action="store_true", help="also fail while the download queue holds jobs")
    args = p.parse_args(argv)

    damaged, jobs = check(args.config)
    for d in damaged:
        print(f"✗ {d.path} 已损坏（{d.reason}）：{OUTCOME[d.policy]}")
    if damaged:
        print("状态文件有问题，先修复或从备份恢复，再部署。")
        return 1
    print("状态文件正常。")
    print(f"下载队列：{jobs} 个任务。")
    if jobs and args.require_idle:
        print("下载队列还有任务，等处理完再部署。")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
