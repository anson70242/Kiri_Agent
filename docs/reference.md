# Kiri Agent: reference

How each part works, in detail: settings, files, rules and guarantees. For an overview (in Chinese) see the
[README](../README.md); for setting up and running a server see [deploy.md](deploy.md) (in Chinese).

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
| [Drive copies on Quark](#drive-copies-on-quark) | Processed live → one zip (video, chat, SRT, highlight table) kept for good on a Quark cloud drive, with a drive link for the editors; set per streamer | Done |
| [Group files to the drive](#group-files-to-the-drive) | A group file (posted in a QQ group) that a filing rule keeps, by the tag in its name (【待校】 and the like) → the rule's subfolder of the group folder on the same Quark drive; set per group. The bot answers two commands, `订阅` (subscriptions) and `帮助` (help) | Done |
| [Flash transfers](#flash-transfers) | Processed live → its files on QQ's flash transfer (闪传), made by an outside flash service; 「全部处理完成」 (all processing done) and the source record sheet give its flash link (14 days) in place of the short download link; set per streamer | Done |
| Google Sheets | The same upload, to Google Sheets instead | Planned |

Each live goes through the steps in order: download → transcribe → analysis and translation side by side → upload → drive copy and flash transfer side by side. Every step retries on failure and reports on Discord.

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
see [backend/llm.py](../backend/llm.py). With `"proxy": true`, only LLM requests go through `LLM_PROXY`:
[backend/llm_proxy.py](../backend/llm_proxy.py) running on another machine, reachable only over Tailscale and protected by a password.
It forwards HTTPS to the official LLM APIs, and it can also forward plain HTTP to a whitelisted local gateway on that machine
(by default `127.0.0.1:8317`, for example the CLIProxyAPI used for Google agy), so `base_url:
http://127.0.0.1:8317/v1` means that machine's localhost. Downloading and monitoring still connect directly.
If you do not need the proxy, set `"proxy": false` and the program connects to `base_url` directly. For setup, see the section 「LLM 代理」 (LLM proxy) in [docs/deploy.md](deploy.md) (in Chinese).

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

- [roster.py](../backend/monitor/roster.py): the roster and each streamer's settings.
- [base.py](../backend/monitor/base.py): the shared `Streamer`, `StreamStatus`, `LiveState`,
  the `PlatformMonitor` base class and the `StreamMonitor` polling.
- [youtube.py](../backend/monitor/youtube.py): `YouTubeMonitor`, scrapes the `/<channel>/live` page; no API key needed.
- [twitch.py](../backend/monitor/twitch.py): `TwitchMonitor`, reads the JSON-LD on the channel page; no API key needed.
- [twitcasting.py](../backend/monitor/twitcasting.py): `TwitCastingMonitor`, reads `data-is-onlive` and similar markers on the channel page; no API key needed.

To add a platform: write a `backend/monitor/<platform>.py` containing a `PlatformMonitor` subclass,
set `platform = "<name>"`, implement `_check()`, and import it in [`__init__.py`](../backend/monitor/__init__.py).

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

What it cannot do (for example, a whole live that starts and ends while the monitor is down is missed) is listed in [docs/monitor-limitations.md](monitor-limitations.md) (in Chinese).

1. In the Discord channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. Put it in `.env`: `DISCORD_WEBHOOK_URL=...`.
3. `uv run -m backend.notify` sends a test message (`--live` announces the streamers who are live right now).

Optional: `notify.discord.mention` in `config.json` can be set to `@everyone`, `@here` or a role `<@&id>`.

- [base.py](../backend/notify/base.py): the `Notifier` base class and the trigger rules of `LiveNotifier`.
- [discord.py](../backend/notify/discord.py): `DiscordNotifier`, webhook embeds with title, cover and avatar.
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

Once a Twitch live has ended, its live preview shows only Twitch's camera placeholder, so its messages, in Discord and in QQ groups, show another picture: 「下播了」 (ended) and the messages before the replay is downloaded show the streamer's avatar,
and from 「回放已下载」 (replay downloaded) on they show the replay's own cover, or the avatar when Twitch has none yet. The picture is kept with the live in the download queue, so a message after a restart shows the same one. YouTube and TwitCasting messages are unchanged.

Mention the bot with `订阅` to see the group's subscriptions (answered for the group's owner and admins), or with `帮助` (or anything else) for the list of commands.

- [qq.py](../backend/notify/qq.py): message format, which groups to send to, cover upload, `QQGroups`.
- [qq_gateway.py](../backend/notify/qq_gateway.py): the WebSocket connection to QQ, group registration, reconnecting.

To add a notification channel: write a `Notifier` subclass, set `name = "<channel>"`, implement `send()`,
import it in [`backend/notify/__init__.py`](../backend/notify/__init__.py), and add a `notify.<channel>` setting.

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
Super Chats / stickers have `sc`); see [chat_clean.py](../backend/downloader/chat_clean.py):

```json
[
  {"time": "00:07:15", "user": "@bob", "msg": "こんゆかし :_chu:"},
  {"time": "01:06:40", "user": "@spica", "msg": "おめでとう", "sc": "¥320"}
]
```

`uv run -m backend.downloader --clean <file>.live_chat.json ...` cleans files downloaded earlier.

- Members-only / age-restricted: after a failure, the download is retried with cookies, from `download.cookies_file` (`data/youtube_cookies.txt`) first,
  otherwise from `download.cookies_from_browser` (default `firefox`; leave it empty to use none). The server has no browser,
  so it uses the cookie file: see 「YouTube 会限 cookie」 (YouTube cookies) in [docs/deploy.md](deploy.md) (in Chinese).
- Twitch subscriber-only replays: set `TWITCH_OAUTH` in `.env` (the `auth-token` cookie from twitch.tv).
- TwitCasting: replays always require a login, so every download sends the cookies from `download.twitcasting_cookies_file` (`data/twitcasting_cookies.txt`);
  see 「TwitCasting 回放要登录 cookie」 (TwitCasting login cookies) in [docs/deploy.md](deploy.md) (in Chinese). Only replay URLs `twitcasting.tv/<ID>/movie/<数字>` (数字 = a number) are accepted;
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

From code: `download(url, *load_settings(config))` in [backend/downloader/__init__.py](../backend/downloader/__init__.py).
A new platform is a `PlatformDownloader` subclass
with `platform`, `url_re` and `download_chat()`.

## Auto-download

When the monitor decides a live has ended (by the same rules as the Discord 下播了 message), and the streamer has auto-download switched on (set in the gear ⚙),
the replay is queued for download (video up to `download.max_height`, default 1080p, plus chat).
The web server or `uv run -m backend.monitor` runs the queue. See [auto.py](../backend/downloader/auto.py).

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
([services/whisper](../services/whisper), faster-whisper large-v3-turbo, started on the GPU by `docker-compose.yml`,
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

After transcription, the job enters the analysis stage ([analysis.py](../backend/analysis.py); the prompts are adapted from AutoKiri's clip_highlight).
It uses the `llm` settings (see LLM proxy).

1. For each 5-minute segment (with 30 seconds of extra context on each side): the transcript + a chat summary (message count, density relative to the average of the whole live, top reactions,
   Super Chats, a sample of up to 150 messages; bot and waiting-room messages are left out) → a topic log, usually 3 entries of 1–3 minutes each.
2. One call for the whole live: all topic logs + chat density → a score of 1–10 for each segment (relative to the rest of the live);
   segments that reach `analysis.min_score` (7) also get highlight moments and a best clip (🏆).
3. The result is `<stem>.highlights.json`, one row per segment: `start`, `end`, `topics`, `timeline`, `highlights`,
   `top_pick`, `score`, `reason`, `chat_count`, `chat_ratio`, `chat_level`
   (高 / 中 / 低, high / medium / low: the top 20% / bottom 30% by density rank).

A singing live (its title contains 歌枠, 歌回, カラオケ, 弾き語り or karaoke, in any letter case) gets every song named in the topic log
of the row where it starts, as `【song name】 原唱：original artist` (topic 唱歌; `【未识别】` with a heard lyric line when the song could not be named).
This takes one extra call per singing live, after step 1, to the model in `analysis.song_model` (`gemini-3.8-flash-high`), with web search,
through the same gateway, key and proxy switch as `llm`. Remove the key or leave it empty to turn song naming off;
if the call fails, the highlight table is made without song entries.

The highlight table is shown on the 路灯表 (highlight tables) page and can be downloaded as Excel (see [Web pages](#web-pages)). Discord gets the 5 highest-scoring segments.
A 5-hour live takes about 3 minutes with `gemini-3-flash`. By hand: `uv run -m backend.analysis <video> --streamer NAME --title T`
(add `--no-proxy` when running it directly on the proxy machine).

## Chinese transcript

After transcription, this runs side by side with the highlight table analysis ([translate.py](../backend/translate.py), using AutoKiri's translation prompt
without the parts written for particular streamers). The transcript is sent to the LLM in groups (`translate.group`, 80 lines), with `translate.context` (5) lines of context on each side;
the reply `{"lines": [{"id", "text"}]}` is matched back line by line, so the timing never drifts. Missing lines are asked for once more; if they are still missing, the Japanese is kept.
The result is `<stem>.zh.transcript.json`. The 录播 (archives) page shows the Chinese transcript on the video by default; the subtitle panel switches between 中 / 日 / 双语 (Chinese / Japanese / both), and SRT can be downloaded.
A 5-hour live takes about 120,000 tokens. By hand: `uv run -m backend.translate <video>`.

## Web pages

`uv run -m backend.monitor.server` (on the server, open it over Tailscale; see [Deployment](#deployment)). Five pages:

- **直播状态** (live status, [monitor.html](../frontend/monitor.html)): who is live / in a waiting room / not live, with covers and tag search.
  「＋ 新增主播」 (add streamer) adds a streamer. The gear ⚙ on each card sets auto-download, how many archives to keep, auto-upload to Tencent Docs and the target spreadsheet,
  auto-upload of drive copies to Quark (自动上传到夸克网盘) and the drive folder, flash transfers (自动上传到 QQ 闪传; greyed out
  while no flash service is configured, and the API refuses to tick it then), and can delete the streamer.
  Settings that are on show on the card as ⬇ 自动下载 (auto-download) / ⬆ 自动上传 (auto-upload) / ☁ 网盘 (drive) / ⚡ 闪传 (flash transfer).
- **录播** ([archives.html](../frontend/archives.html)): downloaded archives and their processing progress (the queue box names each live's stage,
  a flash transfer's as 「等待闪传上传」 (waiting for its flash transfer) and then 「闪传上传 xx%」 while it uploads). Each one can be played,
  with Chinese (default) / Japanese subtitles, a 中 / 日 / 双语 subtitle panel, chat replay and a chat density chart. Downloads: video, chat,
  SRT (Chinese / Japanese), highlight table (Excel), or everything as one zip (without the raw chat); once the live's drive copy is on the drive,
  「☁ 网盘链接」 (drive link) opens it there, and once its flash transfer is uploaded, 「⚡ 闪传链接」 (flash link) opens the flash link until it expires
  (see [Flash transfers](#flash-transfers)).
- **路灯表** ([highlights.html](../frontend/highlights.html)): the highlight table of each archive, filterable by score, chat activity and
  「只看有切片」 (only rows with clips); topic logs can be expanded.
- **通知** ([notify.html](../frontend/notify.html)): the status of the QQ bot and Discord, with test buttons; each QQ group's name,
  on/off switch, subscribed streamers and notification types, and its two group-file switches (see [Group files to the drive](#group-files-to-the-drive)); the latest 50 sent messages;
  and the block 「闪传账号」 (flash account): whether the flash account is online and, while it is not, the QR code to scan (see [Flash transfers](#flash-transfers)).
- **用户** (users, [users.html](../frontend/users.html)): only when login is on, and only for admins; add accounts, reset passwords and disable accounts (see [Login](#login)).

The Excel highlight table ([sheet.py](../backend/sheet.py)) is readable as soon as it is opened: the header row is frozen; the two columns for the content summary and the clip titles are wider, wrap text and
are left-aligned, and the other columns are centred; scores are coloured, and rows with a 🏆 are highlighted in yellow; the last column, 「剪辑」 (editing), is left empty for the team to fill in.
All of this formatting is kept when the file is imported into Tencent Docs or Google Sheets by hand.

API: `/api/status`, `/api/refresh` (POST: check every streamer now), `/api/streamers` (GET / POST / PATCH / DELETE), `/api/downloads`,
`/api/archives/*`, `/api/highlights/*`, `/api/notify*`, `/api/users` (GET / POST / PATCH / DELETE),
`/api/login` / `/api/logout` (POST), `/api/me` (who is logged in, and their role), `/healthz` (health check, open without login)
(see [server.py](../backend/monitor/server.py), [archives.py](../backend/monitor/archives.py),
[notify_api.py](../backend/monitor/notify_api.py) and [auth_api.py](../backend/monitor/auth_api.py)).

## Upload to Tencent Docs

[tencent_docs.py](../backend/tencent_docs.py) uses Tencent Docs' official MCP interface to add the highlight table as a new sub-sheet in an existing spreadsheet,
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

### Source record sheets

A QQ group's source record sheet (录播源记录表) is the group's own Tencent Docs sheet where its editors look up where the files of a live can be fetched.
On the 通知 page, paste the address of one tab of it (`docs.qq.com/sheet/…?tab=…`) into the group's field 「录播源记录表」; an empty field switches it off.
When a live of a streamer ticked for that group is fully processed and 「全部处理完成」 goes out, the service adds a 录播 row to that tab: the sheet's third row
(the first two stay on top), merged like the row below it, with the streamer, the archive's date (`2026/11/5`), 录播, and the live's title with the short download link's expiry,
then the label 「全部下载」 linking to the message's short download link, under the headers 配信者, 日期, 项目类型 and 闪传链接. The address itself is never shown in the sheet.
For a live whose message gave its flash link (see [Flash transfers](#flash-transfers)), that 录播 row holds the flash link instead: the title with the flash link's expiry,
then the label 「闪传链接」 linking to it. A live gets one 录播 row a tab, with whichever link its message gave, never one of each.
When that live's drive copy is uploaded and 「网盘上传完成」 goes out with a drive link (also after a retry), a second row is added the same way, with 网盘 and the live's title (no expiry),
then the label 「网盘链接」 linking to the drive link with its extraction code inside; an upload without a drive link, or a streamer without drive copies, gets no 网盘 row.
Only the field and the ticked streamer count (not the group's switch or notification types); a group the bot was removed from gets nothing, and so does a live whose message has no link (no `SITE_URL` and no flash link).
Rows are written one at a time beside the pipeline, which never waits for them; the log names the group, streamer, live and kind of a row, never its link.
A row that cannot be written is tried again with the pipeline's retry settings (`download.auto.retry_minutes` and `max_errors`: three tries, fifteen minutes apart);
after the last, or at once when the tab lacks one of the four headers, it is given up on and the owner gets one Discord alert naming the group, the streamer,
the live's title, the kind of row and Tencent Docs' reason (with a hint to share the sheet with the token's account when its account may not edit it), without a link. No QQ group is told.
A try that failed after inserting its row never leads to a second row: the retry takes that empty third row, or finds the row written when only the answer was lost.
A row waiting to be written is kept in `data/source_record_jobs.json` (the only place its link is kept), so it is written after a restart and a deploy never waits for it.
Each live (streamer and stream id) gets each kind of row at most once in a tab, also when it is told again or two groups name the same tab: `data/source_record_rows.json` remembers the newest 2000 rows written.
It needs QQ and `TENCENT_DOCS_MCP_TOKEN`, and the token's account must be able to edit the sheet. The code is in [backend/source_record_sheet](../backend/source_record_sheet);
turning it on: 「录播源记录表」 in [docs/deploy.md](deploy.md) (in Chinese).

## Drive copies on Quark

The server keeps only the newest `keep` archives of each streamer. For the streamers whose gear ⚙ has 「自动上传到夸克网盘」 (auto-upload to the Quark drive) ticked,
each live is also kept for good on the owner's Quark cloud drive: once it is fully processed, its drive copy, one zip named like the archive
(`[YYYYMMDD][主播] 标题.zip`) holding the video, the chat, the Chinese and Japanese subtitles as SRT and the highlight table as Excel,
is uploaded into the streamer's drive folder (written in the gear, counted from the top of the drive; blank means `录播/<主播>`, the streamer's name as their archives' folder has it; a missing folder is created).
Then a drive link is made: a Quark share address with its extraction code inside, which does not expire.

- 「全部处理完成」 (all processing done) does not wait for the upload: it goes out with the site's own links, as for any streamer
  (with the flash link when the streamer also has flash transfers: then it waits for the flash transfer, never for the drive copy).
  Only a chat that is still being fetched holds it back (and the upload with it), until the chat is in or given up on.
  When the upload ends, ☁️ 「网盘上传完成」 (drive upload done) brings the drive link, and the archive's page shows 「☁ 网盘链接」 (drive link).
  A QQ group gets that message only when it ticks 「网盘上传完成」 on the 通知 page.
- If the first attempt fails, ❗ 「网盘上传失败」 (drive upload failed) says so; the upload is retried in the background,
  and 「网盘上传完成」 follows when it gets through.
- An archive is not deleted from the server until its drive copy is uploaded or given up on.
- The program never deletes or replaces anything on the drive. A zip whose name is already in the folder is neither built nor uploaded again.
- When the drive's authorization lapses, the owner gets ⚠️ 「网盘授权失效」 (drive authorization lapsed) with the command to run; uploads wait up to 72 hours for it.
- Uploads run one at a time (about 1.5–2.5 hours for a 10 GB live from the server), and a deploy waits for the one that is running.
- `drive.upload_hours` in `config.json` (`[3, 9]`) keeps a drive copy's upload out of the busy hours: it starts only from 03:00 to 09:00 Hong Kong time
  (whatever the server's clock is set to). A live processed outside them waits for the next 03:00, and so does a retry; an upload that is still
  running at 09:00 goes on to its end. 「全部处理完成」 is not held back, and group files go to the drive at any time. Without the setting, an upload starts at any time.

The drive is reached through Quark's official CLI (a Node program), run by `backend.quark`; why, and the risk it carries, is in
[ADR 0001](adr/0001-quark-drive-through-the-official-agent-cli.md). The CLI is in neither this repository nor the image: the owner copies it into the data volume and authorizes it once,
and its authorization lives there as a file, not in `.env`. Without the CLI, ticked streamers are skipped with a warning in the log and everything else works as before.
For the setup, authorizing again, uploading older archives by hand and replacing the CLI, see 「夸克网盘（网盘副本）」 (Quark drive) in [docs/deploy.md](deploy.md) (in Chinese).

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.quark login [--code 授权码]       # 授权（先印出授权链接）
docker compose exec kiri /app/.venv/bin/python -m backend.quark status                    # 是否已授权、剩余空间
docker compose exec kiri /app/.venv/bin/python -m backend.quark upload <录播文件夹> [--folder 网盘文件夹]
```

(The comments mean: authorize (it first prints the authorization link; 授权码 = the authorization code that page gives), and whether it is authorized and the space left. 录播文件夹 = an archive folder or a folder of them, 网盘文件夹 = the drive folder.)

### Group files to the drive

Each QQ group on the 通知 page has two more switches, 「群文件上传到夸克网盘」 (group files to the Quark drive) and 「群文件上传通知」 (group-file notices), both off until turned on and independent of the group's live notifications.
With the first on, a group file (a file posted in the group, in the chat or in its file panel) that one of five filing rules keeps, by a tag in its name and its extension, is uploaded to the same Quark drive:
【待轴】 and 【熟肉】 `.mp4`, 【空轴】, 【待校】 and 【已校】 `.ass`, each into the rule's subfolder of the group folder, `<群名>/<subfolder>/` (`<群名>` is the group's name on the 通知 page, at the top of the drive). Every other file is ignored.

- The group's owner must turn on 「接收所有消息」 (receive all messages) for the bot in mobile QQ; without it the bot sees only the messages that mention it.
  QQ then sends the bot the whole chat; the service acts on attached files and mentions only, and neither stores nor logs anything else
  (why, in [ADR 0002](adr/0002-group-files-through-the-official-bots-message-events.md)).
- A file is downloaded to the server as it is posted, uploaded, and deleted from the server. The same file posted again is not uploaded again; a different file under a name already there is kept beside it, with the time it was posted in its name.
  Nothing on the drive is replaced, moved or deleted.
- Group files and drive copies take turns at the drive (the Quark CLI never runs twice at once), a group file first; behind a running drive copy's upload a group file waits until it ends. Failures are retried like the pipeline's.
- With the second switch on, the group is told when a file is on the drive and when it could not be put there; failures reach Discord either way.
  A group file that has waited a minute behind another upload is told once that it is queued, with what the drive is busy with (a drive copy or another group file) and how far along.
- Out of reach: files already in the group, files posted while the service or the bot is offline (a deploy's restart included), and the folder a member chose in the file panel.

The code is in [backend/group_files](../backend/group_files). For turning it on, see 「群文件上传到夸克网盘」 in [docs/deploy.md](deploy.md) (in Chinese).

## Flash transfers

A **flash transfer** (闪传) is the files of one archive put on QQ's flash transfer, for the streamers whose gear ⚙ ticks 「自动上传到 QQ 闪传」 (auto-upload to QQ flash transfer).
Its **flash link** (闪传链接) is the address editors download it from: served by Tencent's own storage, fast for any QQ user in mainland China without a membership,
and it expires after 14 days (a constant, not a setting). For such a streamer, 「全部处理完成」 (all processing done) and the source record sheet give the flash link
in place of the short download link. The box is independent of the drive box: a streamer can have a flash transfer, a drive copy, both or neither.

Flash transfers are made by **the flash service**: an outside service the owner runs beside this one, logged in with the **flash account** (闪传账号), a QQ account of the owner's.
It is not part of this repository and is not described here; why the feature works this way, and what it risks, is in
[ADR 0003](adr/0003-flash-transfers-through-an-outside-qq-client-service.md). This service calls three of its actions over a WebSocket
(`get_status`, `create_flash_task`, `get_fileset_info`), and its login interface (HTTP) only for the 通知 page's QR code. Every other frame the flash service sends is dropped unread,
so nothing the flash account receives (a group's message) is read, stored or logged; and the flash account posts nothing anywhere.
The client, the only code that knows how the flash service is spoken to, is [backend/flash/client.py](../backend/flash/client.py);
the flash stage is in [auto.py](../backend/downloader/auto.py).

**Settings.** All in the server's `.env` (see [.env.example](../.env.example)), none in `config.json`, which is public and built into the image:

| Variable | What it is |
| --- | --- |
| `FLASH_ACTIONS_URL` | The address of the flash service's actions (`ws://…`). Without it the feature is off: the gear's box is greyed out with 「未设置闪传服务，不能开启」 (no flash service set up) and the streamers API refuses to tick it (422), and everything behaves as before |
| `FLASH_LOGIN_URL` | The address of its login interface (`http://…`) |
| `FLASH_LOGIN_HASH` | What the login interface expects in place of its token (not the token itself) |
| `FLASH_DOWNLOADS_PATH` | How the flash service sees this service's downloads folder; default `/data/downloads` |

Without `FLASH_LOGIN_URL` or `FLASH_LOGIN_HASH`, the account is known only as online or offline (from `get_status`) and the 通知 page shows no QR code, with a line naming the missing setting.
The flash service reads the files from the same downloads volume (read-only is enough). Setting it up: 「QQ 闪传」 in [docs/deploy.md](deploy.md) (in Chinese).

**What is transferred.** What a drive copy holds, as separate files, nothing zipped: the video, the cleaned chat, the Chinese and Japanese transcripts as SRT and the highlight table as Excel,
each left out when the archive lacks it (the raw chat and the JSON files stay out). The flash transfer is named like the archive, `[20261122][森野鈴] 标题`.
The video and the chat are sent from where they are; the SRT and Excel files and a cut video's parts are written into the archive's staging folder,
`.flash_staging/<主播>/<YYYYMMDD>/<标题>/` under the downloads folder (never listed as an archive), which is removed once the flash transfer is uploaded or given up on.
When the service starts, it removes every staging folder left by a kill, except that of a flash transfer already started.

**A video too big for one file.** One file of a flash transfer may be at most 10 GB (QQ's limit). A bigger video is cut with ffmpeg, by stream copy (no re-encoding, minutes of work),
into the fewest parts of equal length whose expected size is under 9.5 GB; each part begins at a key frame and plays by itself. A part that still comes out over 10 GB (an uneven bit rate)
makes the cutting start again with one part more, up to three more. Each part is named with its number, the count and where in the live it begins and ends, read from the parts made
(dots in place of colons, which a file name cannot hold): `[20261122][森野鈴] 标题 (2／3 03.12.45–06.20.10).mp4`. A video at or under 10 GB goes up as it is.

**The message, the sheet and the page.**

- When nothing but the drive copy is left for a live of a ticked streamer, and a flash service is configured, a flash job is queued (in the same save as the drive job).
  The tick counts when the live's processing ends: unticking leaves the flash links already made, and a flash job already queued goes on.
- The flash job waits for a late chat as 「全部处理完成」 does, then prepares the files, starts the flash transfer (its id, flash link and expiry are kept in the job in
  `data/download_queue.json`) and asks for its progress every 10 seconds. A 7.7 GB video took about five minutes from the server.
- Once uploaded, the flash link is noted in the archive (`<stem>.flash_link.json`: the link, its expiry, when the transfer was made), and 「全部处理完成」 goes out with
  「闪传链接（影片、弹幕、中日字幕 SRT、路灯表）」 (flash link: video, chat, SRT, highlight table) in place of 「全部下载」, then 「录播页面」 (archive page, only with `SITE_URL`),
  and the line `闪传链接有效至 2026/12/7` (the flash link works until, the day in Hong Kong time). The rest of the message is unchanged. No short download link is made for that live.
  There is no separate notification type: a group gets this message only when it ticks 全部处理完成, as before.
- The source record sheet's 录播 row holds the flash link behind 「闪传链接」, with its expiry (see [Source record sheets](#source-record-sheets)); the 网盘 row is as before.
  Without `SITE_URL` the message still carries the flash link, and the sheet is told it.
- The archive's page shows 「⚡ 闪传链接」 until it expires (its expiry on hover); the archives API gives `flash_link` (`{url, expires, made}`) until then and `null` after.
  The page's own 全部下载 zip stays.
- The drive copy does not wait for the flash transfer: its upload hours and 「网盘上传完成」 (drive upload done) are as before. A drive copy that ends first sends only 「网盘上传完成」;
  「全部处理完成」 is the flash job's to send.
- One flash transfer at a time, in the order the lives were processed. An archive is not deleted from the server (see `keep`) while its flash job is in the queue.

**When it fails.** A try fails when the flash service refuses or answers an error, a file fails, the bytes sent do not move for 30 minutes, or the files cannot be prepared or cut
(then the reason is 「切分影片失败（详情见日志）」 or 「准备闪传的文件失败（详情见日志）」, and ffmpeg's own words, or the system's, are in the log).
It is tried again with the pipeline's retry settings (`download.auto.retry_minutes` and `max_errors`: three tries, fifteen minutes apart), each try a new flash transfer;
a failing one waits for its next try without holding back other lives. After the last try, 「全部处理完成」 goes out as for a streamer without flash transfers
(the short download link; the sheet is told it), and the owner gets one Discord alert: the streamer, the live's date and title, the reason (without any address),
and the command that makes the flash transfer by hand. No QQ group is told. A flash job whose flash service is no longer configured after a restart is dropped the same way, without the alert,
once its live's late chat is in or given up (the files a flash transfer it had started was reading are removed).

**When the flash account is offline.** An offline flash account and a flash service that cannot be reached are one case (they cannot be told apart from here). The flash job waits:
neither a try nor an error, and nothing is prepared or cut before the account is online. It is looked at every minute.
The owner gets a Discord alert when the first live starts to wait, 「闪传账号掉线或闪传服务连不上」 (flash account offline or flash service unreachable) with how many lives wait
and the 通知 page's address (with `SITE_URL`), and again every 24 hours while any live waits. Never more often: while a live that an alert covered is still in the queue,
a new wait (the account back for a while, then offline again) brings no alert before 24 hours have passed since the last one. One alert covers every waiting live. No QQ group is told.
After the scan nothing more is needed: the waiting ends once the flash service serves again (a flash transfer started, or a started one's progress answered), and the lives go on one at a time in their order
(a flash transfer started before the account went offline goes on by its id), each with its own 「全部处理完成」 and flash link.
A status that merely says online does not end the waiting: while the flash service still cannot start a flash transfer, the files are prepared (and a big video cut) at most once every
retry pause (`download.auto.retry_minutes`), and a started flash transfer is never started again.
A live that has waited 72 hours since it first waited, with no flash transfer served in between, is given up on: 「全部处理完成」 with the short download link, the sheet told it,
and no alert of a failure (the reminders said why). A restart keeps the waiting time and the time of the last reminder.

**The 「闪传账号」 block** on the 通知 page (admins only, like the rest of the page) shows the state, 在线 / 掉线 / 连不上闪传服务 / 未设置 (online / offline / unreachable / not set up),
the flash service's own error text, a missing setting, and how many lives wait for the account. While the account is offline it shows the QR code to scan with mobile QQ,
drawn by the backend (with [segno](https://pypi.org/project/segno/)), and a button 「刷新二维码」 (new QR code); while it shows a QR code it asks again every 3 seconds, so it turns to 在线 by itself after the scan.

- `GET /api/notify/flash` → `{state: online | offline | unreachable | unconfigured, qr_image (a data: URI, only while offline), error, missing, waiting}`.
  A flash service that answers with an error counts as `unreachable`, with its words in `error`.
- `POST /api/notify/flash/qr-code` asks for a new QR code and answers as the GET; 404 without a flash service, 503 when the login interface cannot be reached,
  502 when it refuses (as it does while the account is logged in) or is not set up.

**Restarts and deploys.** The upload runs in the flash service, so a restart does not interrupt it: the flash job asks for its progress again by the id kept in the queue,
and never starts it a second time. A flash job without an id prepares its files again. A deploy waits only while a video is being cut for a flash transfer (a restart would start the cutting over);
an uploading or waiting flash transfer never holds a deploy. The deploy check prints `闪传：N 个任务，M 个正在切分影片，K 个在等闪传账号（只有正在切分的会挡住部署）。`
(flash: N jobs, M cutting a video, K waiting for the flash account; only cutting holds a deploy).

A flash link, a QR code's text, the login hash and the flash service's addresses are never written to the log. A try's reason is kept (in the queue, shown in the queue view),
logged and told in an alert with every address in it (`http`, `https`, `ws`, `wss`) left out; an unexpected error is logged by its kind and that text, without a traceback.
The flash link is kept in the queue while its job is there,
then in the archive's flash link file (and in `data/source_record_jobs.json` until its row is written).

**By hand** ([backend/flash/\_\_main\_\_.py](../backend/flash/__main__.py)):

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.flash status                         # 闪传账号在线吗
docker compose exec kiri /app/.venv/bin/python -m backend.flash upload "森野鈴/20261122/标题"   # 给一场录播做闪传
```

(The comments mean: is the flash account online; make the flash transfer of one archive.)
`status` prints the account's state in one line (exit code 0 online, 1 otherwise). `upload` takes an archive's folder, or its folder reference under the downloads folder
(a folder outside the downloads folder is refused); while the flash account is offline it ends at once with nothing prepared, else
it prepares the files (cutting a video too big for one file), starts the flash transfer, waits for it while printing its progress, notes the flash link in the archive
(its page shows 「⚡ 闪传链接」) and prints the flash link with its expiry. It tells no group and writes no sheet row; exit code 1 when the flash transfer could not be made.
Both exit with 2 when no flash service is configured. Only lives processed after the box was ticked are transferred automatically; this command is for older ones and for a live that fell back.

## Local preview

You can check changes to the pages without deploying:

```sh
uv run scripts/pull_archive.py 青葉 --minutes 3    # 从服务器复制一场录播（影片只取 3 分钟）
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

The browser tests of the web pages ([test_monitor_page.py](../tests/test_monitor_page.py), Playwright) need Chromium
(`uv run playwright install chromium`); without it they skip themselves, which is what happens in CI (CI runs only the code tests).

## Deployment

CI ([ci.yml](../.github/workflows/ci.yml)) runs the tests and builds the Docker image on every pull request and on main.
After CI passes on main, [deploy.yml](../.github/workflows/deploy.yml) copies the code to the server over SSH
and runs `docker compose up -d --build`. Before that restart, the new code reads the server's state files without changing them; the deploy stops there,
with the old version still running, if a state file would be refused or set aside, or while the download queue still holds jobs, a drive copy or a group file is uploading,
or a video is being cut for a flash transfer (see 「部署」 (deployment) in [docs/deploy.md](deploy.md), in Chinese). The web pages listen only on the server's localhost and are reached over Tailscale.

```sh
docker compose up -d --build     # 在本地跑容器（需要 .env）
```

(The comment means: run the containers locally; needs `.env`.)

One-time server setup (Docker, deploy user, SSH key, GitHub secrets, Tailscale): [docs/deploy.md](deploy.md) (in Chinese).

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
See [auth.py](../backend/auth.py) and [auth_api.py](../backend/monitor/auth_api.py) (all permission rules are in `required_role`).

One thing works without an account: the 「全部下载」 (download everything) address in 「全部处理完成」 (all processing done) is a short download link,
`<SITE_URL>/d/<code>` with a random code of ten letters and digits.
Whoever holds it can download that one archive's zip for 31 days, and nothing else; the 「录播页面」 (archive page) link beside it still asks for the login.
The link stops working when the archive is deleted from the server (see `keep`). A live whose message gives its flash link gets no short download link
(see [Flash transfers](#flash-transfers)). The links are kept in `data/download_links.json` (the codes only as hashes):
delete that file to cancel every link sent so far.
The first messages (2026-11-22) carried a longer, signed address instead; those keep working until they expire on 2026-12-23.

### Public HTTPS

No domain is needed: Caddy ([caddy/Caddyfile](../caddy/Caddyfile)) requests an IP address certificate from Let's Encrypt (valid for 6 days, renewed automatically)
and forwards to the web pages. It starts only when `COMPOSE_PROFILES=public`, `PUBLIC_IP` and `PUBLIC_BIND_IP` are set in the server's `.env`,
and the router must forward TCP 80 / 443 to the server. For the steps, see 「公网 HTTPS」 (public HTTPS) in [docs/deploy.md](deploy.md) (in Chinese).
**Create the admin and confirm that a login is required before you open the site to the public.**

## Configuration

`config.json` holds every setting that can be committed: `monitor`, `notify`, `llm`, `analysis`, `translate`, `asr`,
`upload`, `download`, `drive`. Secrets go in `.env` (see [.env.example](../.env.example)): the LLM key, `DISCORD_WEBHOOK_URL`,
`TWITCH_OAUTH`, `LLM_PROXY` / `LLM_PROXY_TOKEN`, `TENCENT_DOCS_MCP_TOKEN`, `QQ_BOT_APP_ID` / `QQ_BOT_SECRET`; so do the flash service's settings
(`FLASH_ACTIONS_URL`, `FLASH_LOGIN_URL`, `FLASH_LOGIN_HASH`, `FLASH_DOWNLOADS_PATH`; see [Flash transfers](#flash-transfers)): they say where the flash service is and how to get into it. Everything changed on the web pages is stored in `data/`
(the roster, the queues, the notification state), which a deploy never touches; so are the Quark CLI and its authorization (`data/quark/`).

## License

This project is licensed under the [GNU Affero General Public License v3.0](../LICENSE) (AGPL-3.0). You may use, modify and distribute it freely;
but if you offer a modified version to others as a network service, you must also publish its source code.
The prompts for the highlight table analysis and the translation are adapted from [AutoKiri](https://github.com/anson70242/AutoKiri), which is also AGPL-3.0.
