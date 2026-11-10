# Kiri Agent

A tool-using LLM agent with no dependencies — Python standard library only, no install
step. It speaks the OpenAI chat completions protocol, so it runs against OpenAI or any
compatible endpoint (a local server, a proxy, another provider) by changing one config
value.

## Setup

```sh
cp .env.example .env      # then put your key in it
```

Edit `config.json` to pick your model and endpoint. Then:

```sh
python backend/agents/agent.py "list the knowledge base and tell me what is there"
python backend/agents/agent.py                    # interactive session
python backend/agents/agent.py --agent reader "summarize backend/"
```

## Tools

| Tool | What it does |
| --- | --- |
| `read_file` | Line-numbered read, with `offset`/`limit` paging |
| `write_file` | Write a file, creating parent directories |
| `edit_file` | Exact string replacement; refuses ambiguous matches unless `replace_all` |
| `list_dir` | Directory listing with sizes |
| `change_dir` | Move the agent's working directory |
| `bash` | Run a shell command; covers mkdir, mv, rm, git, scripts |

Every path is resolved against the agent's current directory and confined to the
workspace, so `../../..` returns an error the model can recover from rather than
reaching the rest of the disk. Set `KIRI_WORKSPACE` to point that root elsewhere.

Adding a tool is one decorated function in [tools.py](backend/agents/tools.py); the
decorator registers the schema and the dispatcher picks it up automatically.

## Agents and subagents

An agent is defined entirely by a profile — a system prompt, a set of tools, and
optionally its own model — so the same `Agent` class serves as the main agent and as
any subagent. Prompts live one-per-file in
[backend/agents/prompts/](backend/agents/prompts/); profiles live in `config.json`:

```json
"agents": {
  "main":   { "prompt": "main.md", "tools": "*" },
  "reader": { "prompt": "reader.md",
              "tools": ["read_file", "list_dir"],
              "model": "gpt-4o-mini" }
}
```

Adding a subagent means adding a prompt file and a profile entry — no code change.
In Python, `Agent.named(cfg, "reader")` gives you that agent.

## Live monitor

Checks whether streamers are live. Environment is managed by uv (`uv sync`).

```sh
uv run -m backend.monitor              # poll forever, print state changes
uv run -m backend.monitor --once       # one check, table output
uv run -m backend.monitor --once --json
uv run -m backend.monitor.server       # web dashboard at http://127.0.0.1:8770
```

Streamers go under `monitor.streamers` in `config.json`. `platform` is `youtube` or
`twitch`; `channel` is a YouTube handle (`@foo`), channel id (`UC...`) or URL, or a
Twitch login (`foo`) or URL.

- [base.py](backend/monitor/base.py): shared `Streamer`, `StreamStatus`, `LiveState`,
  `PlatformMonitor` base class, `StreamMonitor` poll loop.
- [youtube.py](backend/monitor/youtube.py): `YouTubeMonitor`, scrapes `/<channel>/live`; no API key.
- [twitch.py](backend/monitor/twitch.py): `TwitchMonitor`, reads the channel page's JSON-LD; no API key.

New platform: add `backend/monitor/<platform>.py` with a `PlatformMonitor` subclass
setting `platform = "<name>"` and implementing `_check()`, then import it in
[`__init__.py`](backend/monitor/__init__.py).

## Go-live notifications

When a monitored streamer goes from offline / upcoming to live, the monitor (both the
dashboard server and `uv run -m backend.monitor`) sends a Discord message. Streamers
already live at startup are skipped (`notify.on_startup: true` to change that), and the
same stream is never announced twice.

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

`config.json` holds everything safe to commit; `.env` holds the key. `api_key_env`
names the environment variable to read, so switching providers is a config edit.

| Key | Purpose |
| --- | --- |
| `model` | Default model for agents that do not override it |
| `base_url` | API root; `/chat/completions` is appended |
| `api_key_env` | Environment variable holding the key |
| `max_tokens`, `temperature` | Sampling settings |
| `max_steps` | Tool-call round trips allowed per turn, so a loop cannot run away |
| `request_timeout` | Seconds per HTTP request |
| `default_agent` | Profile used when `--agent` is omitted |