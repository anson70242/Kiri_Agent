---
status: accepted
---

# Drive copies reach Quark through its official agent CLI, run as a subprocess

A drive copy is uploaded by running Quark's official `quarkclouddrive` CLI (a closed-source Node bundle, version 1.0.25) as a subprocess of the backend. Quark publishes no API for a personal server, and the owner already pays for an SVIP account with 6 TB, so the choice was between this CLI, a community driver and a different storage provider. The owner chose the CLI on 2026-11-18.

The CLI refuses to run outside an AI agent host it recognises (error `-104`). The backend is not one, so it sets the marker of one (`CLAUDECODE=1`) on the subprocess. This passes a restriction Quark put there on purpose and reports a false channel to Quark. The owner accepted that knowingly: Quark can tighten the check at any time, and the account carries the risk.

## Considered options

- **A community Quark driver** (OpenList or AList as a sidecar, logged in with a browser cookie). Also unofficial, and the cookie expires, but it does not claim to be an agent and others track Quark's changes. Rejected by the owner in favour of the CLI.
- **Object storage with a real API** (Backblaze B2, Cloudflare R2). The stable choice, and the one recommended during design. Rejected because it costs extra per GB and leaves the SVIP space unused.
- **Rewriting the client in Python.** Rejected: it means reverse-engineering a minified bundle against an undocumented API.

## Consequences

- The CLI is not in this repository (public) and is not downloaded at build time. The owner copies one pinned version into the data volume by hand, and it never updates itself: `install.sh` is never run. The image only gains Node. With no CLI or no authorization on the server, uploading is skipped with a warning.
- If Quark stops accepting this CLI version or this marker, uploads fail and say so. Nothing is lost quietly, because an archive is not deleted locally until its drive copy is uploaded or given up on.
- Authorization is a one-time code exchanged by `login --token`. The CLI then keeps and refreshes its own token file, so the credential is a writable file in the data volume, not a value in `.env`.

Measured from the server (Hong Kong) on 2026-11-18, and the reason for several rules in the design:

- A 5 GB file uploads in 58 minutes (1.2 to 2 MB/s). A file over 4 GB is accepted on SVIP. Files over 5 GB were not tried.
- An interrupted upload does not continue. `upload resume` sends the whole file again under the same task, and needs `--file-path`. It was interrupted twice, at 34% and 47% of a 300 MB file. So an upload in progress holds back a deploy.
- Uploading a name that already exists adds `name(1).zip` beside it, even with the same content, and the CLI cannot delete. So the folder is listed before every upload, and a zip already there with the same name and size is not sent again. A retry is a new upload after that check, not `upload resume`.
- The CLI fails an upload when one batch takes over 60 seconds. On a slow route (Japan to the Shenzhen storage node) every file over a few MB failed that way.
- Without `--parent-fid`, files land under `来自：ClaudeCode/`.
- How long an authorization lasts was not measured.
