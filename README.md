# Kiri Agent

Kiri Agent helps a clipping group find material in the lives of VTubers and other streamers. It watches their channels,
downloads the replay automatically when a live ends, transcribes it, translates the transcript into Chinese,
and then builds a highlight table (路灯表): a topic log and a score for every 5-minute segment, plus the chat density,
so the people who edit clips know where to look.
The highlight table can be uploaded automatically to a shared online spreadsheet (Tencent Docs; Google Sheets is planned) so the team can split up the work.
A person still decides what to cut in the end.

## Pipeline

| Step | What it does | Status |
| --- | --- | --- |
| [Live monitoring](#live-monitoring) | Polls YouTube / Twitch / TwitCasting channels; streamers are managed on the web page | Done |
| [Live notifications](#live-notifications) | Sends went-live, ended and processing results to Discord and to QQ groups (official bot) | Done |
| [Downloader](#downloader) | Downloads replays and chat with yt-dlp and Twitch GQL; members-only / subscriber-only replays need cookies | Done |
| Chat cleaning | Raw chat → `{"time","user","msg"}`, one message per line | Done |
| [Auto-download](#auto-download) | Live ended → download the replay, delete the oldest archive, report on Discord | Done |
| [Transcription](#transcription) | Archive → `<stem>.transcript.json` (faster-whisper on a GPU) | Done |
| [Highlight table analysis](#highlight-table-analysis) | 5-minute topic logs, chat density, scores across the whole live, highlight moments → highlight table | Done |
| [Chinese transcript](#chinese-transcript) | Transcript (Japanese) → `<stem>.zh.transcript.json`, shown on the video, downloadable as SRT | Done |
| [Web pages](#web-pages) | Five pages: live status / archives / highlight tables / notifications / users; a highlight table downloads as a formatted Excel file | Done |
| [Upload to Tencent Docs](#upload-to-tencent-docs) | Highlight table → one sub-sheet (formatted) in a Tencent Docs spreadsheet, set per streamer, uploaded automatically | Done |
| Google Sheets | The same upload, to Google Sheets instead | Planned |

Each live goes through the steps in order: download → transcribe → analysis and translation side by side → upload. Every step retries on failure and reports on Discord.

## Installation

```sh
uv sync
cp .env.example .env      # 再填入要用到的值
```

(The comment in the block above means "then fill in the values you need".)

You need Python 3.13+, [uv](https://docs.astral.sh/uv/) and ffmpeg (for downloading).
To run the browser tests, install Chromium once first: `uv run playwright install chromium`.

## LLM proxy

The LLM used for analysis is set in `config.json` → `llm` (`base_url`, `model`, `api_key_env`, `proxy`);
see [backend/llm.py](backend/llm.py). With `"proxy": true`, only LLM requests go through `LLM_PROXY`:
[backend/llm_proxy.py](backend/llm_proxy.py) running on another machine, reachable only over Tailscale and protected by a password.
It forwards HTTPS to the official LLM APIs, and it can also forward plain HTTP to a whitelisted local gateway on that machine
(by default `127.0.0.1:8317`, for example the CLIProxyAPI used for Google agy), so `base_url:
http://127.0.0.1:8317/v1` means that machine's localhost. Downloading and monitoring still connect directly.
If you do not need the proxy, set `"proxy": false` and the program connects to `base_url` directly. For setup, see the section 「LLM 代理」 (LLM proxy) in [docs/deploy.md](docs/deploy.md) (in Chinese).

## Live monitoring

Checks whether streamers are live.

```sh
uv run -m backend.monitor              # 一直轮询，打印状态变化
uv run -m backend.monitor --once       # 只查一次，输出表格
uv run -m backend.monitor --once --json
uv run -m backend.monitor.server       # 网页：http://127.0.0.1:8770
```

(The comments above mean: poll continuously and print state changes; check once and print a table; web page.)

Streamers are managed on the web page (see [Web pages](#web-pages)). The roster is stored in `data/streamers.json` (`monitor.streamers_file`),
which a deploy never touches; `monitor.streamers` in `config.json` is used only on the first start, to create the roster.
`platform` is `youtube`, `twitch` or `twitcasting`. `channel` is a YouTube handle (`@foo`), channel ID (`UC...`) or URL;
a Twitch login name (`foo`) or URL; or a TwitCasting ID (`foo`, `@foo`, `c:foo`) or URL. When you add a streamer, the program first checks that the channel exists; deleting a streamer keeps the archives already downloaded.

- [roster.py](backend/monitor/roster.py): the roster and each streamer's settings.
- [base.py](backend/monitor/base.py): the shared `Streamer`, `StreamStatus`, `LiveState`,
  the `PlatformMonitor` base class and the `StreamMonitor` polling.
- [youtube.py](backend/monitor/youtube.py): `YouTubeMonitor`, scrapes the `/<channel>/live` page; no API key needed.
- [twitch.py](backend/monitor/twitch.py): `TwitchMonitor`, reads the JSON-LD on the channel page; no API key needed.
- [twitcasting.py](backend/monitor/twitcasting.py): `TwitCastingMonitor`, reads `data-is-onlive` and similar markers on the channel page; no API key needed.

To add a platform: write a `backend/monitor/<platform>.py` containing a `PlatformMonitor` subclass,
set `platform = "<name>"`, implement `_check()`, and import it in [`__init__.py`](backend/monitor/__init__.py).

## Live notifications

When a streamer goes from not live / waiting room to live, the monitor (both the web server and `uv run -m backend.monitor`) sends a Discord message (and, if the QQ bot is set up, a message to the subscribed QQ groups; see below).
On the very first start (no `data/notify_state.json` yet), streamers who are already live are not announced (`notify.on_startup: true` changes that);
streamers added later are announced even if they are already live. The same sitting (one or more lives in a row by the same streamer that count as one, for example after a reconnect) is never announced twice.

When a live ends, the streamer must stay not live for `notify.offline_grace` seconds (default 120) before the 下播了 (ended) message is sent, with the duration of the live,
so a short disconnect does not cause a false alarm. `notify.on_offline: false` turns the 下播了 message off.

Guarantees (all covered by tests):
- Every 开播了 (went live) message is followed by exactly one 下播了 message for the same sitting (a live is identified by its `stream_id`: the YouTube video ID, the Twitch start time, the TwitCasting movie ID).
- A failed check (network error, bot-verification page) is never taken as "ended".
- Each streamer's last live is stored in `data/notify_state.json`, so a live that started or ended while the monitor was down is reported after the restart.
  Run only one monitor process at a time, because they would share this file.
- Messages leave a queue in order; a failure is retried up to 5 times (with growing intervals) and never slows down polling. A rejected webhook (4xx) is only logged, not retried.

What it cannot do (for example, a whole live that starts and ends while the monitor is down is missed) is listed in [docs/monitor-limitations.md](docs/monitor-limitations.md) (in Chinese).

1. In the Discord channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. Put it in `.env`: `DISCORD_WEBHOOK_URL=...`.
3. `uv run -m backend.notify` sends a test message (`--live` announces the streamers who are live right now).

Optional: `notify.discord.mention` in `config.json` can be set to `@everyone`, `@here` or a role `<@&id>`.

- [base.py](backend/notify/base.py): the `Notifier` base class and the trigger rules of `LiveNotifier`.
- [discord.py](backend/notify/discord.py): `DiscordNotifier`, webhook embeds with title, cover and avatar.
  Discord gets every message, including all failures and alerts (for example, the QQ bot being offline). Footers: went-live / ended messages carry 「切片小助手 直播通知」 (live notice),
  download, transcription and translation messages carry 「切片小助手 自动下载」 (auto-download), and highlight table analysis and upload messages carry 「切片小助手 自动路灯」 (auto highlight table).

### QQ groups (official bot)

An official bot from the QQ Open Platform (q.qq.com) posts to QQ groups. Each group subscribes to its own streamers and notification types (set on the 通知 (notifications) page).

1. Create a bot on q.qq.com and, under 「开发设置」 (development settings), choose WebSocket for events. Put the AppID / AppSecret in the server's `.env`
   as `QQ_BOT_APP_ID` and `QQ_BOT_SECRET`, then run `docker compose up -d --force-recreate kiri`.
   kiri stays connected to QQ the whole time (the bot shows as online); no public URL is needed.
2. Add the bot to a group (an unverified bot can only join groups you own), or @-mention the bot in a group: the group appears on the 通知 page automatically,
   and the bot replies in the group with that group's current subscriptions. A new group starts switched off, with no streamers subscribed.
3. On the 通知 page, name the group, tick the streamers (by person: a YouTube, a Twitch and a TwitCasting entry with the same name count as one) and the notification types (by default 开播 (went live), 下播 (ended), 上传完成 (upload done) and 全部处理完成 (all processing done)), switch notifications on, and save.

Messages are Markdown. The second line of a went-live message is the live's cover image (Tencent cannot fetch images from YouTube, so we download it and upload it to the bot's media library,
which holds at most 100 images; when it is full, our oldest one is deleted), followed by a clickable link to the live; an upload-done message includes the sub-sheet link. When sending to one group fails (for example, the group has turned off
the bot's proactive messages), it is only recorded under 「最近记录」 (recent log) and does not affect other groups. When the bot is removed from a group or the group turns off proactive messages, sending to that group stops
and Discord gets one alert; if the bot cannot connect for 10 minutes in a row, Discord also gets one alert. Group settings are stored in `data/qq_groups.json`,
and the send log (the latest 50 entries) in `data/notify_log.json`.

- [qq.py](backend/notify/qq.py): message format, which groups to send to, cover upload, `QQGroups`.
- [qq_gateway.py](backend/notify/qq_gateway.py): the WebSocket connection to QQ, group registration, reconnecting.

To add a notification channel: write a `Notifier` subclass, set `name = "<channel>"`, implement `send()`,
import it in [`backend/notify/__init__.py`](backend/notify/__init__.py), and add a `notify.<channel>` setting.

## Downloader

Downloads YouTube / Twitch / TwitCasting replays and chat. No external executables are needed: video and metadata come from the `yt-dlp` Python package
(the nightly pre-release, because YouTube changes often), whose JavaScript runtime is the `deno` pip package; Twitch chat comes straight from
Twitch's GQL API, and TwitCasting chat is read from the public comment history page. The only extra install is ffmpeg (on the PATH, or set `download.ffmpeg`).

```sh
uv run -m backend.downloader <url>              # 影片 + 弹幕
uv run -m backend.downloader <url> --no-video   # 只要弹幕（--no-chat：只要影片）
uv run -m backend.downloader <url> --info       # 只看资料
uv lock --upgrade-package yt-dlp && uv sync     # 更新到最新的 yt-dlp nightly
```

(The comments above mean: video + chat; chat only (`--no-chat`: video only); metadata only; update to the latest yt-dlp nightly.)

Files are saved to `downloads/<主播>/<YYYYMMDD>/<标题>/[日期][主播] 标题.mp4` (主播 = the creator, 标题 = the title, 日期 = the date), with the chat next to the video:
`.live_chat.json` (YouTube, yt-dlp's raw format), `.chat.json` (Twitch, TwitchDownloaderCLI's format) or
`.comments.json` (TwitCasting, the comments read from the comment history page).
For channels in the roster, the creator is the name in the roster; for other channels, it is the channel's display name.

Every downloaded chat is also cleaned into `.chat_parsed.json` (AutoKiri's format, one message per line; only YouTube
Super Chats / stickers have `sc`); see [chat_clean.py](backend/downloader/chat_clean.py):

```json
[
  {"time": "00:07:15", "user": "@bob", "msg": "こんゆかし :_chu:"},
  {"time": "01:06:40", "user": "@spica", "msg": "おめでとう", "sc": "¥320"}
]
```

`uv run -m backend.downloader --clean <file>.live_chat.json ...` cleans files downloaded earlier.

- Members-only / age-restricted: after a failure, the download is retried with cookies, from `download.cookies_file` (`data/youtube_cookies.txt`) first,
  otherwise from `download.cookies_from_browser` (default `firefox`; leave it empty to use none). The server has no browser,
  so it uses the cookie file: see 「YouTube 会限 cookie」 (YouTube cookies) in [docs/deploy.md](docs/deploy.md) (in Chinese).
- Twitch subscriber-only replays: set `TWITCH_OAUTH` in `.env` (the `auth-token` cookie from twitch.tv).
- TwitCasting: replays always require a login, so every download sends the cookies from `download.twitcasting_cookies_file` (`data/twitcasting_cookies.txt`);
  see 「TwitCasting 回放要登录 cookie」 (TwitCasting login cookies) in [docs/deploy.md](docs/deploy.md) (in Chinese). Only replay URLs `twitcasting.tv/<ID>/movie/<数字>` (数字 = a number) are accepted;
  the archive title is the live's own title followed by its start time `HHmmss`. Lives protected by a 合言葉 (password) and members-only lives are not supported.
- A live that is still on air cannot be downloaded. Right after a YouTube live ends, YouTube is still processing it, and it cannot be downloaded until that finishes either (run it again later).

YouTube's chat replay often appears only several hours after the live ends. Until it does, the chat is queued in `data/pending_chat.json`
and retried every 30 minutes for up to 48 hours (`download.chat_retry`); once downloaded, it is put next to the video under the original file name.

```sh
uv run -m backend.downloader --pending          # 列出排队中的弹幕
uv run -m backend.downloader --retry            # 现在全部重试
uv run -m backend.downloader --wait             # 按排程重试，直到队列清空
uv run -m backend.downloader <url> --wait       # 下载后，等晚到的弹幕
```

(The comments above mean: list the queued chats; retry them all now; retry on schedule until the queue is empty; after downloading, wait for the late chat.)

From code: `download(url, *load_settings(config))` in [backend/downloader/__init__.py](backend/downloader/__init__.py).
A new platform is a `PlatformDownloader` subclass
with `platform`, `url_re` and `download_chat()`.

## Auto-download

When the monitor decides a live has ended (by the same rules as the Discord 下播了 message), and the streamer has auto-download switched on (set in the gear ⚙),
the replay is queued for download (video up to `download.max_height`, default 1080p, plus chat).
The web server or `uv run -m backend.monitor` runs the queue. See [auto.py](backend/downloader/auto.py).

- Not ready yet (YouTube still processing, Twitch replay not listed yet): retried every `download.auto.retry_minutes` (15) minutes,
  for up to `give_up_hours` (12) hours, then the failure is reported on Discord.
- TwitCasting replay not published: if the live lasted 5 minutes or more (or has no start time), one 「回放未公开」 (replay not published) notification is sent, then the download is retried at the interval above;
  if the replay is published in the meantime, it is downloaded as usual. If it is still not published after `give_up_hours`, the program gives up quietly, without reporting a failure. For a live shorter than 5 minutes, no notification is sent and the program gives up at once.
- Other failures (expired cookies, deleted video, network) are retried `max_errors` (3) times, then reported on Discord;
  when the failure looks like expired cookies, the message includes a hint.
- Success: the Discord message (and the one to subscribed QQ groups) includes the file size, the chat status, and a link to this archive's page on the site (only when `SITE_URL` is set in the server's `.env`, for example `https://<IP>` once the site is public). Then only `keep` archives are kept in this streamer's folder
  (settable per streamer, default `download.auto.keep` = 6), oldest deleted first. If deleting old archives fails (for example, a file is open in another program),
  the new archive is processed as usual, and Discord gets a separate 「旧录播没能删除」 (old archives could not be deleted) alert (a QQ group receives it only if that type is ticked on the 通知 page).
- The queue is stored in `data/download_queue.json`, so it resumes after a restart (a deploy); late chat goes through the chat retry queue described above.

## Transcription

When an auto-download finishes, the same job enters the transcription stage. Downloading and transcription each have one worker (one download and one transcription at a time, running side by side),
so a long transcription never holds up the next download. ffmpeg extracts the audio (16 kHz mono Opus), and the audio of the whole live is sent to the `whisper` service
([services/whisper](services/whisper), faster-whisper large-v3-turbo, started on the GPU by `docker-compose.yml`,
reachable only from kiri). The transcript is saved next to the video:

```json
[
  {"start": 0.0, "end": 2.0, "text": "ちょっと話がまじで噛み合わへんくて"},
  {"start": 3.0, "end": 5.0, "text": "なんか味せえへんやんみたいな"}
]
```

It runs at about 13× real time on a GTX 1660 (about 20 minutes for a 4-hour live). A failure is retried `download.auto.max_errors` times,
then Discord gets 「字幕转写失败」 (transcription failed). Settings are in the `asr` section of `config.json` (`url`, `language`, `vad`;
remove the section to turn transcription off). By hand: `uv run -m backend.asr <video> --url http://...:8000`.

## Highlight table analysis

After transcription, the job enters the analysis stage ([analysis.py](backend/analysis.py); the prompts are adapted from AutoKiri's clip_highlight).
It uses the `llm` settings (see LLM proxy).

1. For each 5-minute segment (with 30 seconds of extra context on each side): the transcript + a chat summary (message count, density relative to the average of the whole live, top reactions,
   Super Chats, a sample of up to 150 messages; bot and waiting-room messages are left out) → a topic log, usually 3 entries of 1–3 minutes each.
2. One call for the whole live: all topic logs + chat density → a score of 1–10 for each segment (relative to the rest of the live);
   segments that reach `analysis.min_score` (7) also get highlight moments and a best clip (🏆).
3. The result is `<stem>.highlights.json`, one row per segment: `start`, `end`, `topics`, `timeline`, `highlights`,
   `top_pick`, `score`, `reason`, `chat_count`, `chat_ratio`, `chat_level`
   (高 / 中 / 低, high / medium / low: the top 20% / bottom 30% by density rank).

The highlight table is shown on the 路灯表 (highlight tables) page and can be downloaded as Excel (see [Web pages](#web-pages)). Discord gets the 5 highest-scoring segments.
A 5-hour live takes about 3 minutes with `gemini-3-flash`. By hand: `uv run -m backend.analysis <video> --streamer NAME --title T`
(add `--no-proxy` when running it directly on the proxy machine).

## Chinese transcript

After transcription, this runs side by side with the highlight table analysis ([translate.py](backend/translate.py), using AutoKiri's translation prompt
without the parts written for particular streamers). The transcript is sent to the LLM in groups (`translate.group`, 80 lines), with `translate.context` (5) lines of context on each side;
the reply `{"lines": [{"id", "text"}]}` is matched back line by line, so the timing never drifts. Missing lines are asked for once more; if they are still missing, the Japanese is kept.
The result is `<stem>.zh.transcript.json`. The 录播 (archives) page shows the Chinese transcript on the video by default; the subtitle panel switches between 中 / 日 / 双语 (Chinese / Japanese / both), and SRT can be downloaded.
A 5-hour live takes about 120,000 tokens. By hand: `uv run -m backend.translate <video>`.

## Web pages

`uv run -m backend.monitor.server` (on the server, open it over Tailscale; see [Deployment](#deployment)). Five pages:

- **直播状态** (live status, [monitor.html](frontend/monitor.html)): who is live / in a waiting room / not live, with covers and tag search.
  「＋ 新增主播」 (add streamer) adds a streamer. The gear ⚙ on each card sets auto-download, how many archives to keep, auto-upload to Tencent Docs and the target spreadsheet,
  and can delete the streamer. Settings that are on show on the card as ⬇ 自动下载 (auto-download) / ⬆ 自动上传 (auto-upload).
- **录播** ([archives.html](frontend/archives.html)): downloaded archives and their processing progress. Each one can be played,
  with Chinese (default) / Japanese subtitles, a 中 / 日 / 双语 subtitle panel, chat replay and a chat density chart. Downloads: video, chat,
  SRT (Chinese / Japanese), highlight table (Excel), or everything as one zip (without the raw chat).
- **路灯表** ([highlights.html](frontend/highlights.html)): the highlight table of each archive, filterable by score, chat activity and
  「只看有切片」 (only rows with clips); topic logs can be expanded.
- **通知** ([notify.html](frontend/notify.html)): the status of the QQ bot and Discord, with test buttons; each QQ group's name,
  on/off switch, subscribed streamers and notification types; the latest 50 sent messages.
- **用户** (users, [users.html](frontend/users.html)): only when login is on, and only for admins; add accounts, reset passwords and disable accounts (see [Login](#login)).

The Excel highlight table ([sheet.py](backend/sheet.py)) is readable as soon as it is opened: the header row is frozen; the two columns for the content summary and the clip titles are wider, wrap text and
are left-aligned, and the other columns are centred; scores are coloured, and rows with a 🏆 are highlighted in yellow; the last column, 「剪辑」 (editing), is left empty for the team to fill in.
All of this formatting is kept when the file is imported into Tencent Docs or Google Sheets by hand.

API: `/api/status`, `/api/refresh` (POST: check every streamer now), `/api/streamers` (GET / POST / PATCH / DELETE), `/api/downloads`,
`/api/archives/*`, `/api/highlights/*`, `/api/notify*`, `/api/users` (GET / POST / PATCH / DELETE),
`/api/login` / `/api/logout` (POST), `/api/me` (who is logged in, and their role), `/healthz` (health check, open without login)
(see [server.py](backend/monitor/server.py), [archives.py](backend/monitor/archives.py),
[notify_api.py](backend/monitor/notify_api.py) and [auth_api.py](backend/monitor/auth_api.py)).

## Upload to Tencent Docs

[tencent_docs.py](backend/tencent_docs.py) uses Tencent Docs' official MCP interface to add the highlight table as a new sub-sheet in an existing spreadsheet,
placed first (the latest archive is always the first sub-sheet). The columns, text and formatting are the same as the Excel download: frozen header, column widths,
centred / left-aligned, wrapped text, score colours, 🏆 rows in yellow.

Auto-upload is set per streamer: in the gear ⚙, tick 「自动上传到腾讯文档」 (auto-upload to Tencent Docs) and paste the link of the target spreadsheet (each streamer can have its own spreadsheet,
or several can share one). The highlight table is uploaded as soon as it is ready (a stage after analysis: like every other stage it retries,
and Discord gets 📤 or ❗ with the sub-sheet link). Uploading the same archive again replaces the old sub-sheet. By hand:

```sh
uv run -m backend.tencent_docs downloads/<主播>/<YYYYMMDD>/<标题>/ --sheet <链接或 FILE_ID>
uv run -m backend.tencent_docs <.highlights.json> --sheet <链接> --title "20261111 xx"
```

(In the commands above, 主播 = the creator, 标题 = the title, 链接 = the link, 链接或 FILE_ID = the link or the FILE_ID.)

Credentials (shared by all streamers): a personal MCP token. Scan the QR code at <https://docs.qq.com/open/auth/mcp.html> with the QQ / WeChat account that owns (or can edit) the spreadsheets to get one;
no developer registration and no mainland Chinese ID card are needed.
Put it in `TENCENT_DOCS_MCP_TOKEN` in `.env`; without it, uploads are skipped (only logged). When the token expires, uploads fail and
Discord gets 「rejected the token」; just get a new one. After changing it on the server, restart with
`docker compose up -d --force-recreate kiri` (a plain restart does not re-read `.env`).

Details: the sub-sheet name is `<YYYYMMDD> <主播> <标题>` (date, creator, title), cut to 31 characters. Uploading again first deletes the old sub-sheet; only when it is the only
sub-sheet in the whole spreadsheet does the new one get ` (2)` appended. Uploading one archive takes about 10 seconds and about 10 API calls, plus one per run of consecutive rows with the same colour
(about 50 calls for a 5-hour live). A free account gets 2000 calls a day (according to the official MCP page), enough for dozens of archives a day.

## Local preview

You can check changes to the pages without deploying:

```sh
uv run scripts/pull_archive.py 鹿乃 --minutes 3    # 从服务器复制一场录播（影片只取 3 分钟）
uv run -m backend.monitor.server --dev --reload    # http://127.0.0.1:8770
```

(The first comment means: copy one archive from the server, taking only 3 minutes of the video.)

`--dev` shows the pages (and live status) but does not download, transcribe or analyse anything, and sends nothing to Discord. After changing HTML, refresh the browser to see it;
`--reload` restarts the server when the backend code changes. `pull_archive.py` copies every file except the raw chat over ssh into
`downloads/` (`--minutes 0`: no video, `--full`: the whole video).

## Tests

```sh
uv run pytest                          # 离线测试，跳过慢的压力测试（约 50 秒）
uv run pytest -m "not network"         # 全部离线测试，CI 跑的就是这个
uv run ruff check                      # 检查没用到的 import 和变量
uv run pytest -m network -s            # 连真实的 YouTube / Twitch / TwitCasting 检查
STRESS_SEEDS=1000 uv run pytest tests/test_random_sequences.py   # 更长的压力测试
```

(The comments above mean: offline tests, skipping the slow stress test (about 50 seconds); all offline tests, which is what CI runs; check for unused imports and variables; check against the real YouTube / Twitch / TwitCasting; a longer stress test.)

The browser tests of the web pages ([test_monitor_page.py](tests/test_monitor_page.py), Playwright) need Chromium
(`uv run playwright install chromium`); without it they skip themselves, which is what happens in CI (CI runs only the code tests).

## Deployment

CI ([ci.yml](.github/workflows/ci.yml)) runs the tests and builds the Docker image on every pull request and on main.
After CI passes on main, [deploy.yml](.github/workflows/deploy.yml) copies the code to the server over SSH
and runs `docker compose up -d --build`. Before that restart, the new code reads the server's state files without changing them; the deploy stops there,
with the old version still running, if a state file would be refused or set aside, or while the download queue still holds jobs (see 「部署」 (deployment) in [docs/deploy.md](docs/deploy.md), in Chinese). The web pages listen only on the server's localhost and are reached over Tailscale.

```sh
docker compose up -d --build     # 在本地跑容器（需要 .env）
```

(The comment means: run the containers locally; needs `.env`.)

One-time server setup (Docker, deploy user, SSH key, GitHub secrets, Tailscale): [docs/deploy.md](docs/deploy.md) (in Chinese).

### Login

`"auth": {"enabled": true}` in `config.json` turns login on (the local preview with `--dev` never asks for a login). There are two roles:

| | Admin | Member |
|---|---|---|
| View live status, archives and highlight tables; download video / subtitles / highlight table / zip | ✅ | ✅ |
| Add or change streamers, gear settings, the 通知 page, the 用户 page | ✅ | ❌ |

Create the first admin on the server (you type the password twice):

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.auth add-user <名字> --admin
```

(名字 = the name.)

After that, add accounts, reset passwords and disable accounts on the 用户 page. There are also `list` / `passwd <名字>` / `disable <名字>` / `enable <名字>`.
Until the first admin exists, every page shows only this command. A login lasts 30 days; 5 wrong passwords from the same IP lock it out for 15 minutes;
disabling a person or changing their password logs them out at once. Accounts are stored in `data/users.json` (passwords as scrypt hashes) and logins in `data/sessions.json`.
See [auth.py](backend/auth.py) and [auth_api.py](backend/monitor/auth_api.py) (all permission rules are in `required_role`).

### Public HTTPS

No domain is needed: Caddy ([caddy/Caddyfile](caddy/Caddyfile)) requests an IP address certificate from Let's Encrypt (valid for 6 days, renewed automatically)
and forwards to the web pages. It starts only when `COMPOSE_PROFILES=public`, `PUBLIC_IP` and `PUBLIC_BIND_IP` are set in the server's `.env`,
and the router must forward TCP 80 / 443 to the server. For the steps, see 「公网 HTTPS」 (public HTTPS) in [docs/deploy.md](docs/deploy.md) (in Chinese).
**Create the admin and confirm that a login is required before you open the site to the public.**

## Configuration

`config.json` holds every setting that can be committed: `monitor`, `notify`, `llm`, `analysis`, `translate`, `asr`,
`upload`, `download`. Secrets go in `.env` (see [.env.example](.env.example)): the LLM key, `DISCORD_WEBHOOK_URL`,
`TWITCH_OAUTH`, `LLM_PROXY` / `LLM_PROXY_TOKEN`, `TENCENT_DOCS_MCP_TOKEN`, `QQ_BOT_APP_ID` / `QQ_BOT_SECRET`. Everything changed on the web pages is stored in `data/`
(the roster, the queues, the notification state), which a deploy never touches.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0). You may use, modify and distribute it freely;
but if you offer a modified version to others as a network service, you must also publish its source code.
The prompts for the highlight table analysis and the translation are adapted from [AutoKiri](https://github.com/anson70242/AutoKiri), which is also AGPL-3.0.
