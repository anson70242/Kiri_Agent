# Kiri Agent

Helps people cut highlight clips from VTuber / streamer broadcasts. It watches channels,
downloads each archive when a stream ends, and analyses the VOD so an editor knows
where to look: segment-by-segment summaries and scores, plus chat activity as a signal
for the exciting moments. The result is uploaded to a shared online sheet (Google Sheets,
Tencent Docs) so a team can split up the clipping work. A human still makes the final cut.

## Pipeline

| Step | What it does | Status |
| --- | --- | --- |
| [Live monitor](#live-monitor) | Polls YouTube / Twitch channels, web dashboard | done |
| [Notifications](#go-live-notifications) | Discord message on go-live and stream end | done |
| [Downloader](#downloader) | VOD + chat replay via yt-dlp and Twitch GQL; members / sub-only with cookies | done |
| Chat cleaning | Raw chat → `{"time","user","msg"}` lines | done |
| [Auto download](#auto-download) | Stream ends → archive downloaded, old ones pruned, Discord report | done |
| Chat density | Messages per minute, spikes, Super Chats → candidate highlight times | planned |
| [Transcription](#transcription) | Downloaded VOD → `<stem>.transcript.json` via faster-whisper on the GPU | done |
| [Segment analysis](#analysis-路灯表) | 5-minute topic logs, whole-stream scoring, highlights → `<stem>.highlights.json` | done |
| Highlight report | Ranked timeline for editors | planned |
| Sheet upload | Push the report to Google Sheets / Tencent Docs (docs.qq.com) | planned |

## Setup

```sh
uv sync
cp .env.example .env      # then fill in what you use
```

Python 3.13+, [uv](https://docs.astral.sh/uv/), and ffmpeg (for downloads).

## Agent

[backend/agents/agent.py](backend/agents/agent.py) is the LLM side, built on the
OpenAI Agents SDK over the chat completions API, so any OpenAI-compatible endpoint works
(OpenAI, a local llama.cpp / vLLM / Ollama, ...). It is still a scaffold; the analysis
agents will live here. Set `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` in `.env`:

```sh
uv run backend/agents/agent.py "hello"     # one-shot
uv run backend/agents/agent.py             # interactive session
```

### LLM proxy

The analysis LLM is set in `config.json` → `llm` (`base_url`, `model`, `api_key_env`,
`proxy`), see [backend/llm.py](backend/llm.py). With `"proxy": true`, only LLM requests
go through `LLM_PROXY`: another machine running [backend/llm_proxy.py](backend/llm_proxy.py),
reachable only over Tailscale and password-protected. It tunnels HTTPS to the official
LLM API hosts, and forwards plain HTTP to allowlisted local gateways on that machine
(default `127.0.0.1:8317`, e.g. CLIProxyAPI for Google agy), so `base_url:
http://127.0.0.1:8317/v1` means that machine's localhost. Downloads and monitoring keep
connecting directly. `"proxy": false` (for anyone who doesn't need it) connects straight
to `base_url`. Setup: "LLM 代理" in [docs/deploy.md](docs/deploy.md).

## Live monitor

Checks whether streamers are live. Environment is managed by uv (`uv sync`).

```sh
uv run -m backend.monitor              # poll forever, print state changes
uv run -m backend.monitor --once       # one check, table output
uv run -m backend.monitor --once --json
uv run -m backend.monitor.server       # web dashboard at http://127.0.0.1:8770
```

Streamers are managed on the dashboard (直播状态 page: ＋ 新增主播, and the ⚙ gear on
each card for auto-download, archives to keep, and deleting). The list is saved in
`data/streamers.json` (`monitor.streamers_file`), which deploys never touch;
`monitor.streamers` in `config.json` only seeds it on the first start. `platform` is
`youtube` or `twitch`; `channel` is a YouTube handle (`@foo`), channel id (`UC...`) or
URL, or a Twitch login (`foo`) or URL. Deleting a streamer keeps their downloads.

- [roster.py](backend/monitor/roster.py): the streamer list and per-streamer settings.

- [base.py](backend/monitor/base.py): shared `Streamer`, `StreamStatus`, `LiveState`,
  `PlatformMonitor` base class, `StreamMonitor` poll loop.
- [youtube.py](backend/monitor/youtube.py): `YouTubeMonitor`, scrapes `/<channel>/live`; no API key.
- [twitch.py](backend/monitor/twitch.py): `TwitchMonitor`, reads the channel page's JSON-LD; no API key.

New platform: add `backend/monitor/<platform>.py` with a `PlatformMonitor` subclass
setting `platform = "<name>"` and implementing `_check()`, then import it in
[`__init__.py`](backend/monitor/__init__.py).

## Go-live notifications

When a monitored streamer goes from offline / upcoming to live, the monitor (both the
dashboard server and `uv run -m backend.monitor`) sends a Discord message. On the very
first start (no `data/notify_state.json` yet) streamers already live are skipped
(`notify.on_startup: true` to change that); a streamer added to the config later is
announced even if already live. The same stream is never announced twice.

When a stream ends it sends an "ended" message with the stream's duration, after the
streamer stays not-live for `notify.offline_grace` seconds (default 120), so a brief
blip sends nothing. `notify.on_offline: false` turns ended messages off.

Guarantees (covered by the tests):
- Every go-live is followed by exactly one ended for the same broadcast (`stream_id`:
  YouTube video id, Twitch start time). A broadcast is never announced twice.
- A failed check (network error, bot-check page) never counts as "offline".
- The last live broadcast per streamer is saved in `data/notify_state.json`, so a
  stream that ended or started while the monitor was down is reported after restart.
  Run only one monitor process at a time, since they would share this file.
- Messages are sent in order from a queue, retried up to 5 times with backoff, and
  never slow down polling. A rejected webhook (4xx) is logged and not retried.

What it can't do (e.g. a whole stream that starts and ends while the monitor is down
is missed) is listed in [docs/monitor-limitations.md](docs/monitor-limitations.md).

1. Discord channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. Put it in `.env` as `DISCORD_WEBHOOK_URL=...`.
3. `uv run -m backend.notify` sends a test message (`--live` announces whoever is live now).

Optional `notify.discord.mention` in `config.json`: `@everyone`, `@here` or a role `<@&id>`.

- [base.py](backend/notify/base.py): `Notifier` base class, `LiveNotifier` trigger rules.
- [discord.py](backend/notify/discord.py): `DiscordNotifier`, webhook embed with title, cover, avatar.

New channel: add a `Notifier` subclass with `name = "<channel>"` and `send()`, import it
in [`backend/notify/__init__.py`](backend/notify/__init__.py), add a `notify.<channel>` entry.

## Downloader

Downloads a YouTube / Twitch VOD and its chat replay. No external exe: video and
metadata use the `yt-dlp` Python package (nightly pre-release, since YouTube changes
often), its JS runtime is the `deno` pip package, and Twitch chat comes straight from
Twitch's GQL API. Only ffmpeg must be installed (on PATH, or `download.ffmpeg`).

```sh
uv run -m backend.downloader <url>              # video + chat
uv run -m backend.downloader <url> --no-video   # chat only (--no-chat: video only)
uv run -m backend.downloader <url> --info       # metadata only
uv lock --upgrade-package yt-dlp && uv sync     # update to the latest yt-dlp nightly
```

Files go to `downloads/<streamer>/<YYYYMMDD>/<title>/[date][streamer] title.mp4`, with
chat next to it: `.live_chat.json` (YouTube, raw yt-dlp format) or `.chat.json` (Twitch,
TwitchDownloaderCLI's layout). A channel listed in `monitor.streamers` uses that
streamer's name; any other channel uses its display name.

Every downloaded chat is also cleaned into `.chat_parsed.json` (AutoKiri's format, one
message per line; `sc` only on YouTube Super Chats / Stickers), see
[chat_clean.py](backend/downloader/chat_clean.py):

```json
[
  {"time": "00:07:15", "user": "@bob", "msg": "こんゆかし :_chu:"},
  {"time": "01:06:40", "user": "@spica", "msg": "おめでとう", "sc": "¥320"}
]
```

`uv run -m backend.downloader --clean <file>.live_chat.json ...` cleans files downloaded earlier.

- Members-only / age-restricted: a failed attempt is retried with cookies, from
  `download.cookies_file` (`data/youtube_cookies.txt`) if it exists, else from
  `download.cookies_from_browser` (default `firefox`; empty to disable). A server has
  no browser, so it uses the cookies file: see "YouTube 会限 cookie" in
  [docs/deploy.md](docs/deploy.md).
- Twitch subscriber-only VODs: set `TWITCH_OAUTH` in `.env` (the `auth-token` cookie of twitch.tv).
- Live streams are refused. Right after a YouTube stream ends it is still processing:
  the video is refused until it's done (run again later).

YouTube chat replay often appears only hours after a stream ends. When it isn't there
yet, the chat is queued in `data/pending_chat.json` and retried every 30 min, for up to
48 h (`download.chat_retry`). It lands next to the video under the original file name.

```sh
uv run -m backend.downloader --pending          # list queued chats
uv run -m backend.downloader --retry            # retry them all now
uv run -m backend.downloader --wait             # retry on schedule until the queue is empty
uv run -m backend.downloader <url> --wait       # download, then wait for a late chat
```

From code: `download(url, *load_settings(config))` in
[backend/downloader/__init__.py](backend/downloader/__init__.py). A new platform is a
`PlatformDownloader` subclass with `platform`, `url_re` and `download_chat()`.

## Auto download

When the monitor reports a stream ended (same rule as the "ended" Discord message), the
archive of a streamer with auto-download on (the ⚙ gear, or `"auto_download": true`) is queued and
downloaded (video ≤ `download.max_height`, default 1080p, plus chat) by
the dashboard server or `uv run -m backend.monitor`. See
[auto.py](backend/downloader/auto.py).

- Not ready yet (YouTube still processing, Twitch archive not listed): retried every
  `download.auto.retry_minutes` (15) for up to `give_up_hours` (12).
- Other failures (expired cookies, deleted video, network) are retried `max_errors` (3)
  times, then Discord gets a failure message, with a hint when cookies look expired.
- Success: Discord message with file size and chat status. Then that streamer's folder
  is trimmed to `keep` archives (per streamer, default `download.auto.keep` = 6); the
  oldest go first.
- The queue is `data/download_queue.json`, so a restart (deploy) resumes it; late chats
  use the chat retry queue below.

## Transcription

After an auto-download, the same queue job moves to a "transcribe" stage. Downloads and
transcriptions have a worker each (one download + one transcription at a time, side by
side), so a long transcription never holds up the next download. ffmpeg
extracts the audio (16 kHz mono Opus) and the whole stream is sent to the `whisper`
service ([services/whisper](services/whisper), faster-whisper large-v3-turbo, started by
`docker-compose.yml` on the GPU, reachable only from kiri). Result next to the video:

```json
[
  {"start": 0.0, "end": 2.0, "text": "ちょっと話がまじで噛み合わへんくて"},
  {"start": 3.0, "end": 5.0, "text": "なんか味せえへんやんみたいな"}
]
```

About 13× realtime on a GTX 1660 (a 4 h stream in ~20 min). Failures are retried
`download.auto.max_errors` times, then Discord gets a "字幕转写失败" message. Configure
with the `asr` section of `config.json` (`url`, `language`, `vad`; remove it to skip
transcription). By hand: `uv run -m backend.asr <video> --url http://...:8000`.

## Analysis (路灯表)

After transcription the job moves to an "analyze" stage ([analysis.py](backend/analysis.py),
prompts adapted from AutoKiri's clip_highlight). Uses the `llm` settings (see LLM proxy).

1. Every 5-minute segment (plus 30 s context each side): transcript + a chat digest (count,
   density vs. the stream average, top reactions, Super Chats, up to 150 sampled messages;
   bots and waiting-room chat excluded) → a topic log, usually 3 entries of 1–3 minutes.
2. One call over the whole stream: all topic logs + chat density → a 1–10 score per segment,
   relative within the stream; segments from `analysis.min_score` (7) also get highlights
   and a best clip (🏆).
3. `<stem>.highlights.json`, one row per segment: `start`, `end`, `topics`, `timeline`,
   `highlights`, `top_pick`, `score`, `reason`, `chat_count`, `chat_ratio`, `chat_level`
   (高/中/低: top 20% / bottom 30% of segments by density).

Discord then gets the top 5 segments. A 5 h stream takes about 3 minutes with
`gemini-3-flash`. By hand: `uv run -m backend.analysis <video> --streamer NAME --title T`
(`--no-proxy` when running on the proxy machine itself).

## Chinese subtitles

Runs side by side with the analysis once the transcript exists ([translate.py](backend/translate.py),
AutoKiri's translator prompt without its streamer-specific parts). The transcript goes
to the LLM in groups (`translate.group`, 80 lines) with `translate.context` (5) lines of
context each side; the reply `{"lines": [{"id", "text"}]}` maps back 1:1, so timing never
moves. Missing lines are asked again, then kept in Japanese. Output
`<stem>.zh.transcript.json`; the archives page shows it on the video (default track),
switches 中 / 日 / 双语 in the subtitle panel, and offers SRT downloads. A 5 h stream is
about 120k tokens. By hand: `uv run -m backend.translate <video>`.

## Local preview

Check page changes without deploying:

```sh
uv run scripts/pull_archive.py 鹿乃 --minutes 3    # copy an archive from the server (3 min of video)
uv run -m backend.monitor.server --dev --reload    # http://127.0.0.1:8770
```

`--dev` shows the pages (and live status) but never downloads, transcribes, analyzes
or sends Discord messages. HTML changes show on a browser refresh; `--reload` restarts
the server when backend code changes. `pull_archive.py` streams everything but the raw
chat over ssh (`--minutes 0`: no video, `--full`: whole video) into `downloads/`.

## Tests

```sh
uv run pytest                          # everything offline (~40s)
uv run pytest -m network -s            # real YouTube / Twitch checks
STRESS_SEEDS=1000 uv run pytest tests/test_random_sequences.py   # longer stress run
```

## Deploy

CI ([ci.yml](.github/workflows/ci.yml)) runs the tests and builds the Docker image on
every pull request and on main. When CI passes on main, [deploy.yml](.github/workflows/deploy.yml)
copies the code to the server over SSH and runs `docker compose up -d --build`.
The dashboard listens only on the server's localhost and is reached through Tailscale.

```sh
docker compose up -d --build     # run the container locally (needs .env)
```

One-time server setup (Docker, deploy user, SSH key, GitHub secrets, Tailscale):
[docs/deploy.md](docs/deploy.md).

## Configuration

`config.json` holds everything safe to commit (`monitor`, `notify`, `download`);
secrets go in `.env` (see [.env.example](.env.example)): LLM endpoint and key,
`DISCORD_WEBHOOK_URL`, `TWITCH_OAUTH`, `LLM_PROXY` / `LLM_PROXY_TOKEN`.