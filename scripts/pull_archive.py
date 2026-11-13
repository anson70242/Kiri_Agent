"""Copy one archive from the server into ./downloads for a local preview.

    uv run scripts/pull_archive.py                  # newest archive, first 10 min of video
    uv run scripts/pull_archive.py 鹿乃 --minutes 3  # newest whose path contains "鹿乃"
    uv run scripts/pull_archive.py 鹿乃 --minutes 0  # no video (subtitles, chat, 路灯表 only)
    uv run scripts/pull_archive.py 鹿乃 --full       # whole video (GBs)

Then: uv run -m backend.monitor.server --dev --reload  ->  http://127.0.0.1:8770

Everything except the raw chat comes over (parsed chat, transcripts, 路灯表). The video
is cut on the server with `ffmpeg -c copy`, so a short clip arrives in seconds. The
archive is streamed through ssh as a tar: no temp files on either side.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Runs inside the kiri container (working directory /app, so the deployed backend imports):
# writes a tar of the chosen archive to stdout.
REMOTE = r'''
import os, subprocess, sys, tarfile, tempfile
from pathlib import Path
from backend.archive import DOWNLOADING_SUFFIX, SAVING_SUFFIX, Artifact, Downloads
match, minutes = sys.argv[1], int(sys.argv[2])
found = [a for a in Downloads(Path("/app/downloads")).list() if match in a.ref]  # newest first
if not found:
    sys.exit(f"no archive matches {match!r}")
archive = found[0]
print(f"pulling {archive.ref}", file=sys.stderr)
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
    for f in sorted(archive.folder.iterdir()):
        if f.suffix in (DOWNLOADING_SUFFIX, SAVING_SUFFIX) or Artifact.RAW_CHAT.matches(f.name):
            continue  # unfinished downloads and saves; raw chat (the parsed chat comes over)
        arc = f"{archive.ref}/{f.name}"
        if Artifact.VIDEO.matches(f.name) and minutes >= 0:
            if minutes == 0:
                continue
            fd, tmp = tempfile.mkstemp(suffix=f.suffix); os.close(fd)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(f), "-t", str(minutes * 60),
                            "-c", "copy", tmp], check=True)
            tar.add(tmp, arcname=arc)
            os.remove(tmp)
            continue
        tar.add(f, arcname=arc)
'''


def main() -> int:
    p = argparse.ArgumentParser(description="Copy an archive from the server for a local preview.")
    p.add_argument("match", nargs="?", default="", help="part of <streamer>/<date>/<title> (default: newest)")
    p.add_argument("--minutes", type=int, default=10, help="video clip length; 0 = no video")
    p.add_argument("--full", action="store_true", help="the whole video")
    p.add_argument("--host", default="HK-Server", help="ssh host (from ~/.ssh/config)")
    p.add_argument("--dir", default="~/kiri", help="deploy directory on the server")
    p.add_argument("--out", type=Path, default=REPO / "downloads")
    args = p.parse_args()

    minutes = -1 if args.full else args.minutes
    cmd = (f"cd {args.dir} && docker compose exec -T kiri /app/.venv/bin/python - "
           f"{shlex.quote(args.match)} {minutes}")
    proc = subprocess.Popen(["ssh", args.host, cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin and proc.stdout
    proc.stdin.write(REMOTE.encode())
    proc.stdin.close()
    args.out.mkdir(parents=True, exist_ok=True)
    names = []
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                tar.extract(member, args.out, filter="data")
                names.append(member.name)
    except tarfile.ReadError:
        pass  # nothing came: the remote side printed why
    if proc.wait() != 0 or not names:
        print("pull failed", file=sys.stderr)
        return 1
    for n in names:
        print(f"  {n}")
    print(f"done -> {args.out}\nnext: uv run -m backend.monitor.server --dev --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
