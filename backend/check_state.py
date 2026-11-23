"""Deploy check: would this code start on the state files that are here, and is the
download queue idle?

    uv run -m backend.check_state [--config config.json] [--require-idle]

It builds the stores the way the service does, inside a dry run (backend.state_file), so
every state file is read with the decoder the service would use and nothing on disk
changes: no backup, no seeded roster, no alert. The deploy workflow runs it in the new
image against the server's data before anything restarts.

Exit code: 0 fine, 1 a state file is damaged, 2 the download queue still holds jobs
(only with --require-idle). Drive jobs (uploads to the cloud drive) do not count, except
the one whose upload is running: a waiting upload is done again after the restart, a
running one would start over from zero. Group files (posted in a QQ group, on their way to
the drive) count the same way: only the one whose upload is running holds a deploy; one
waiting is uploaded after the restart from the copy on the server, and one downloading is
continued or downloaded again. Flash jobs do not count either: a flash transfer uploads in
the flash service, and its job asks for its progress again after the restart. Rows waiting for a source record sheet never hold a deploy:
they are written after the restart.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.auth import load_auth
from backend.downloader import load_settings
from backend.downloader.auto import Job, load_auto
from backend.downloader.pending import PendingChats
from backend.group_files import GroupFileJob, GroupFileJobs, state_files
from backend.group_files.record import GroupFileUploads
from backend.monitor.roster import load_roster
from backend.notify import load_notifier
from backend.source_record_sheet import RowJobs, RowRecord
from backend.source_record_sheet import state_files as source_record_files
from backend.state_file import Damage, dry_run

REPO_ROOT = Path(__file__).resolve().parents[1]

# What the service would do with a damaged file, by its policy.
OUTCOME = {"strict": "新版本会拒绝启动", "recover": "新版本会把它备份后清空"}


def check(config: Path) -> tuple[list[Damage], list[Job], list[GroupFileJob]]:
    """The damaged state files among those the service would load with this config, the
    jobs in the download queue, and the group-file jobs."""
    with dry_run() as damaged:
        load_roster(config)  # the roster
        load_auth(config)  # users, sessions
        notifier = load_notifier(config, allow_empty=True)  # live-stream state, notification history, QQ groups
        auto = load_auto(config)
        jobs = auto.load() if auto else []  # the download queue
        settings, _ = load_settings(config)
        if pending := PendingChats.from_settings(settings):
            pending.load()  # the chat retry queue
        group_files: list[GroupFileJob] = []
        if notifier and notifier.qq:  # the service keeps group files only with the QQ bot on
            jobs_file, uploads_file = state_files(notifier.qq.groups.path)
            group_files = GroupFileJobs(jobs_file).load()
            GroupFileUploads(uploads_file).load()  # the record of uploads
            rows_jobs, rows_file = source_record_files(notifier.qq.groups.path)  # the source record sheets
            RowJobs(rows_jobs).load()
            RowRecord(rows_file).load()  # the record of rows written
    unique = {d.path: d for d in damaged}  # the roster is loaded twice (load_auto reads it too)
    return list(unique.values()), jobs, group_files


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="backend.check_state", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    p.add_argument("--require-idle", action="store_true", help="also fail while the download queue holds jobs")
    args = p.parse_args(argv)

    damaged, queue, group_files = check(args.config)
    for d in damaged:
        print(f"✗ {d.path} 已损坏（{d.reason}）：{OUTCOME[d.policy]}")
    if damaged:
        print("状态文件有问题，先修复或从备份恢复，再部署。")
        return 1
    print("状态文件正常。")
    # A flash job never holds a deploy: its upload runs in the flash service, and the job asks
    # for its progress again after the restart.
    jobs = [j for j in queue if j.stage not in ("drive", "flash")]
    drive = [j for j in queue if j.stage == "drive"]
    uploading = [j for j in drive if j.uploading]
    print(f"下载队列：{len(jobs)} 个任务。")
    print(f"网盘上传：{len(drive)} 个任务，{len(uploading)} 个正在上传（只有正在上传的会挡住部署）。")
    files = [f for f in group_files if f.uploading]
    print(f"群文件：{len(group_files)} 个任务，{len(files)} 个正在上传（只有正在上传的会挡住部署）。")
    if not args.require_idle or not (jobs or uploading or files):
        return 0
    if jobs:
        print("下载队列还有任务，等处理完再部署。")
    for j in uploading:
        print(f"正在上传到网盘：{j.status.streamer.name} {j.status.title or j.status.url}，等它传完再部署。")
    for f in files:
        print(f"正在上传群文件到网盘：{f.group_folder} 群的 {f.name}，等它传完再部署。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
