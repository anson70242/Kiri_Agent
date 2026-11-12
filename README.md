# Kiri Agent

帮切片组从 VTuber / 主播的直播里找素材。它监控频道，下播后自动下载录播，转写字幕、翻译成中文，
再分析出「路灯表」：每 5 分钟一段的话题日志和评分，加上弹幕密度，告诉剪辑的人该看哪里。
路灯表可以自动上传到共享的在线表格（腾讯文档；Google Sheets 计划中），方便团队分工。
最后怎么剪，还是人来决定。

## 流程

| 步骤 | 做什么 | 状态 |
| --- | --- | --- |
| [直播监控](#直播监控) | 轮询 YouTube / Twitch 频道；在网页上管理主播 | 完成 |
| [开播通知](#开播通知) | 开播、下播和处理结果发到 Discord 和 QQ 群（官方机器人） | 完成 |
| [下载器](#下载器) | 用 yt-dlp 和 Twitch GQL 下载录播和弹幕；会员 / 订阅限定靠 cookie | 完成 |
| 弹幕清洗 | 原始弹幕 → `{"time","user","msg"}` 一行一条 | 完成 |
| [自动下载](#自动下载) | 下播 → 下载录播、删除最旧的、Discord 报告 | 完成 |
| [字幕转写](#字幕转写) | 录播 → `<stem>.transcript.json`（GPU 上的 faster-whisper） | 完成 |
| [路灯表分析](#路灯表分析) | 5 分钟话题日志、弹幕密度、全场评分、高能点 → 路灯表 | 完成 |
| [中文字幕](#中文字幕) | 日文字幕 → `<stem>.zh.transcript.json`，显示在影片上，可下载 SRT | 完成 |
| [网页](#网页) | 直播状态 / 录播 / 路灯表 / 通知四个页面；路灯表可下载成排好版的 Excel | 完成 |
| [上传腾讯文档](#上传腾讯文档) | 路灯表 → 腾讯文档表格里的一个子表（带格式），按主播设定、自动上传 | 完成 |
| Google Sheets | 同样的上传，换成 Google Sheets | 计划中 |

每一场直播按顺序跑：下载 → 转写 → 分析和翻译同时进行 → 上传。每一步失败都会重试，并在 Discord 报告。

## 安装

```sh
uv sync
cp .env.example .env      # 再填入要用到的值
```

需要 Python 3.13+、[uv](https://docs.astral.sh/uv/) 和 ffmpeg（下载用）。
要跑浏览器测试的话，先装一次：`uv run playwright install chromium`。

## Agent

[backend/agents/agent.py](backend/agents/agent.py) 是 LLM 这一侧，基于 OpenAI Agents SDK，
走 chat completions API，所以任何兼容 OpenAI 的端点都能用（OpenAI、本地的 llama.cpp / vLLM /
Ollama……）。目前还只是骨架。在 `.env` 设定 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`：

```sh
uv run backend/agents/agent.py "hello"     # 问一次
uv run backend/agents/agent.py             # 互动模式
```

### LLM 代理

分析用的 LLM 设定在 `config.json` → `llm`（`base_url`、`model`、`api_key_env`、`proxy`），
见 [backend/llm.py](backend/llm.py)。`"proxy": true` 时，只有 LLM 请求会经过 `LLM_PROXY`：
另一台机器上跑的 [backend/llm_proxy.py](backend/llm_proxy.py)，只能通过 Tailscale 连接，并且有密码保护。
它把 HTTPS 转发到官方 LLM API，也能把纯 HTTP 转发到那台机器上白名单内的本地网关
（默认 `127.0.0.1:8317`，例如 Google agy 用的 CLIProxyAPI），所以 `base_url:
http://127.0.0.1:8317/v1` 指的是那台机器的 localhost。下载和监控照旧直连。
不需要代理的人设 `"proxy": false`，直接连 `base_url`。设定方法见 [docs/deploy.md](docs/deploy.md) 的「LLM 代理」。

## 直播监控

检查主播是否在直播。

```sh
uv run -m backend.monitor              # 一直轮询，打印状态变化
uv run -m backend.monitor --once       # 只查一次，输出表格
uv run -m backend.monitor --once --json
uv run -m backend.monitor.server       # 网页：http://127.0.0.1:8770
```

主播在网页上管理（见[网页](#网页)）。名单存在 `data/streamers.json`（`monitor.streamers_file`），
部署不会动它；`config.json` 的 `monitor.streamers` 只在第一次启动时用来生成名单。
`platform` 是 `youtube` 或 `twitch`；`channel` 是 YouTube 的 handle（`@foo`）、频道 ID（`UC...`）或网址，
或 Twitch 的登录名（`foo`）或网址。新增主播时会先检查频道是否存在；删除主播会保留已下载的录播。

- [roster.py](backend/monitor/roster.py)：主播名单和各主播的设定。
- [base.py](backend/monitor/base.py)：共用的 `Streamer`、`StreamStatus`、`LiveState`、
  `PlatformMonitor` 基类和 `StreamMonitor` 轮询。
- [youtube.py](backend/monitor/youtube.py)：`YouTubeMonitor`，抓 `/<channel>/live` 页面；不需要 API key。
- [twitch.py](backend/monitor/twitch.py)：`TwitchMonitor`，读频道页的 JSON-LD；不需要 API key。

新增平台：写一个 `backend/monitor/<platform>.py`，里面放一个 `PlatformMonitor` 子类，
设 `platform = "<name>"` 并实现 `_check()`，再在 [`__init__.py`](backend/monitor/__init__.py) 里 import。

## 开播通知

主播从未开播 / 待机变成直播时，监控（网页服务器和 `uv run -m backend.monitor` 都会）发一条 Discord 消息（设了 QQ 机器人的话，也发到订阅的 QQ 群，见下面）。
第一次启动时（还没有 `data/notify_state.json`），已经在播的主播不会提醒（`notify.on_startup: true` 可以改）；
之后才加进来的主播，就算已经在播也会提醒。同一场直播不会提醒两次。

下播后，主播保持未直播 `notify.offline_grace` 秒（默认 120）才发「已下播」消息，附上直播时长，
所以短暂断线不会误报。`notify.on_offline: false` 可以关掉下播消息。

保证（都有测试覆盖）：
- 每次开播之后，同一场直播（`stream_id`：YouTube 的影片 ID、Twitch 的开播时间）一定刚好有一次下播消息。
- 检查失败（网络错误、机器人验证页）永远不会当成「下播」。
- 每个主播最后一场直播存在 `data/notify_state.json`，所以监控停机期间开播或下播的，重启后会补报。
  同一时间只跑一个监控进程，因为它们会共用这个文件。
- 消息从队列按顺序发出，失败最多重试 5 次（间隔递增），不会拖慢轮询。webhook 被拒（4xx）只记日志、不重试。

做不到的事（例如监控停机期间从开播到下播的一整场会漏掉）列在 [docs/monitor-limitations.md](docs/monitor-limitations.md)。

1. Discord 频道设置 → 整合 → Webhooks → 新增 Webhook → 复制 Webhook 网址。
2. 填进 `.env`：`DISCORD_WEBHOOK_URL=...`。
3. `uv run -m backend.notify` 发一条测试消息（加 `--live` 会提醒现在正在播的主播）。

可选：`config.json` 的 `notify.discord.mention` 可以设 `@everyone`、`@here` 或身份组 `<@&id>`。

- [base.py](backend/notify/base.py)：`Notifier` 基类，`LiveNotifier` 的触发规则。
- [discord.py](backend/notify/discord.py)：`DiscordNotifier`，带标题、封面、头像的 webhook embed。
  Discord 收所有消息，包括各种失败和提醒（例如 QQ 机器人离线）。页脚：开播 / 下播是「切片小助手 开播通知」，
  下载、转写、翻译是「切片小助手 自动下载」，路灯表分析和上传是「切片小助手 自动路灯」。

### QQ 群（官方机器人）

用 QQ 开放平台（q.qq.com）的官方机器人发到 QQ 群，每个群各自订阅主播和通知类型（在「通知」页面设定）。

1. 在 q.qq.com 建一个机器人，「开发设置」里事件用 WebSocket；把 AppID / AppSecret 填进服务器的 `.env`：
   `QQ_BOT_APP_ID`、`QQ_BOT_SECRET`，再 `docker compose up -d --force-recreate kiri`。
   kiri 会一直连着 QQ（机器人显示在线），不需要公网网址。
2. 把机器人拉进群（未认证的机器人只能进你当群主的群），或在群里 @机器人：群会自动出现在「通知」页面，
   机器人也会在群里回复本群的订阅现况。新群默认关闭、不订阅任何主播。
3. 在「通知」页面给群取名，勾选主播和通知类型（默认开播、下播、录播已下载、上传完成），打开通知、保存。

消息是 Markdown：开播第二行是直播封面（腾讯下载不到 YouTube 的图，所以由我们下载后传进机器人的素材库，
最多 100 张，满了删掉我们最旧的），附可点击的直播链接；上传完成附子表链接。一个群发送失败（例如群里关了
机器人主动发言）只记在「最近记录」里，不影响其他群。机器人被移出群或群里关闭主动发言时，该群停发，
Discord 收到一次提醒；机器人连续 10 分钟连不上也提醒一次。群设定存在 `data/qq_groups.json`，
发送记录（最近 50 条）存在 `data/notify_log.json`。

- [qq.py](backend/notify/qq.py)：消息格式、发给哪些群、封面上传、`QQGroups`。
- [qq_gateway.py](backend/notify/qq_gateway.py)：和 QQ 的 WebSocket 连线、群的登记、断线重连。

新增通知渠道：写一个 `Notifier` 子类，设 `name = "<channel>"` 并实现 `send()`，
在 [`backend/notify/__init__.py`](backend/notify/__init__.py) 里 import，再加一个 `notify.<channel>` 设定。

## 下载器

下载 YouTube / Twitch 的录播和弹幕回放。不需要外部 exe：影片和资料用 `yt-dlp` Python 套件
（nightly 预发布版，因为 YouTube 常常改），它的 JS 运行环境是 `deno` pip 套件，Twitch 弹幕直接从
Twitch 的 GQL API 拿。只需要另外装 ffmpeg（在 PATH 里，或设 `download.ffmpeg`）。

```sh
uv run -m backend.downloader <url>              # 影片 + 弹幕
uv run -m backend.downloader <url> --no-video   # 只要弹幕（--no-chat：只要影片）
uv run -m backend.downloader <url> --info       # 只看资料
uv lock --upgrade-package yt-dlp && uv sync     # 更新到最新的 yt-dlp nightly
```

文件存到 `downloads/<主播>/<YYYYMMDD>/<标题>/[日期][主播] 标题.mp4`，弹幕放在旁边：
`.live_chat.json`（YouTube，yt-dlp 原始格式）或 `.chat.json`（Twitch，TwitchDownloaderCLI 的格式）。
主播名单里的频道用名单里的名字，其他频道用频道显示名称。

每份下载的弹幕也会清洗成 `.chat_parsed.json`（AutoKiri 的格式，一行一条；只有 YouTube 的
Super Chat / 贴图才有 `sc`），见 [chat_clean.py](backend/downloader/chat_clean.py)：

```json
[
  {"time": "00:07:15", "user": "@bob", "msg": "こんゆかし :_chu:"},
  {"time": "01:06:40", "user": "@spica", "msg": "おめでとう", "sc": "¥320"}
]
```

`uv run -m backend.downloader --clean <file>.live_chat.json ...` 可以清洗以前下载的文件。

- 会员限定 / 年龄限制：失败后会带 cookie 重试，优先用 `download.cookies_file`（`data/youtube_cookies.txt`），
  没有的话用 `download.cookies_from_browser`（默认 `firefox`；留空表示不用）。服务器没有浏览器，
  所以用 cookie 文件：见 [docs/deploy.md](docs/deploy.md) 的「YouTube 会限 cookie」。
- Twitch 订阅限定录播：在 `.env` 设 `TWITCH_OAUTH`（twitch.tv 的 `auth-token` cookie）。
- 正在直播的不能下载。YouTube 刚下播时还在处理，处理完之前也不能下载（晚点再跑）。

YouTube 的弹幕回放常常要下播几小时后才出现。还没出现时，会排进 `data/pending_chat.json`，
每 30 分钟重试一次，最多 48 小时（`download.chat_retry`），下载到后放在影片旁边、沿用原本的文件名。

```sh
uv run -m backend.downloader --pending          # 列出排队中的弹幕
uv run -m backend.downloader --retry            # 现在全部重试
uv run -m backend.downloader --wait             # 按排程重试，直到队列清空
uv run -m backend.downloader <url> --wait       # 下载后，等晚到的弹幕
```

在程序里用：[backend/downloader/__init__.py](backend/downloader/__init__.py) 的
`download(url, *load_settings(config))`。新增平台是一个 `PlatformDownloader` 子类，
要有 `platform`、`url_re` 和 `download_chat()`。

## 自动下载

监控判定下播时（和 Discord「已下播」消息同一套规则），开了自动下载的主播（齿轮 ⚙ 里设定）
的录播会排进队列下载（影片不超过 `download.max_height`，默认 1080p，加弹幕），
由网页服务器或 `uv run -m backend.monitor` 执行。见 [auto.py](backend/downloader/auto.py)。

- 还没好（YouTube 还在处理、Twitch 录播还没列出）：每 `download.auto.retry_minutes`（15）分钟重试，
  最多 `give_up_hours`（12）小时。
- 其他失败（cookie 过期、影片被删、网络）重试 `max_errors`（3）次，然后在 Discord 报失败；
  看起来是 cookie 过期时会附上提示。
- 成功：Discord 消息附上文件大小和弹幕状态。然后这个主播的文件夹只保留 `keep` 场
  （每个主播可以设，默认 `download.auto.keep` = 6），最旧的先删。
- 队列存在 `data/download_queue.json`，所以重启（部署）后会接着跑；晚到的弹幕走上面的弹幕重试队列。

## 字幕转写

自动下载完成后，同一个任务进入「转写」阶段。下载和转写各有一个 worker（一次一个下载、一个转写，同时进行），
所以长时间的转写不会挡住下一场下载。ffmpeg 抽出音频（16 kHz 单声道 Opus），整场送给 `whisper` 服务
（[services/whisper](services/whisper)，faster-whisper large-v3-turbo，由 `docker-compose.yml`
在 GPU 上启动，只有 kiri 连得到）。结果放在影片旁边：

```json
[
  {"start": 0.0, "end": 2.0, "text": "ちょっと話がまじで噛み合わへんくて"},
  {"start": 3.0, "end": 5.0, "text": "なんか味せえへんやんみたいな"}
]
```

GTX 1660 上大约 13 倍速（4 小时的直播约 20 分钟）。失败重试 `download.auto.max_errors` 次，
然后 Discord 收到「字幕转写失败」。在 `config.json` 的 `asr` 段设定（`url`、`language`、`vad`；
删掉这段就不转写）。手动：`uv run -m backend.asr <video> --url http://...:8000`。

## 路灯表分析

转写完后进入「分析」阶段（[analysis.py](backend/analysis.py)，prompt 改编自 AutoKiri 的 clip_highlight）。
用 `llm` 的设定（见 LLM 代理）。

1. 每 5 分钟一段（前后各多带 30 秒上下文）：字幕 + 弹幕摘要（条数、相对全场平均的密度、热门反应、
   Super Chat、最多 150 条抽样；排除机器人和待机室弹幕）→ 话题日志，通常 3 条，每条 1–3 分钟。
2. 全场一次调用：所有话题日志 + 弹幕密度 → 每段 1–10 分（全场内相对比较）；
   达到 `analysis.min_score`（7）分的段落再加上高能点和最佳切片（🏆）。
3. 输出 `<stem>.highlights.json`，每段一行：`start`、`end`、`topics`、`timeline`、`highlights`、
   `top_pick`、`score`、`reason`、`chat_count`、`chat_ratio`、`chat_level`
   （高 / 中 / 低：按密度排名前 20% / 后 30%）。

路灯表会显示在「路灯表」页面，也能下载成 Excel（见[网页](#网页)）。Discord 会收到分数最高的 5 段。
5 小时的直播用 `gemini-3-flash` 大约 3 分钟。手动：`uv run -m backend.analysis <video> --streamer NAME --title T`
（在代理那台机器上直接跑时加 `--no-proxy`）。

## 中文字幕

字幕转写完成后，和路灯表分析同时进行（[translate.py](backend/translate.py)，用 AutoKiri 的翻译 prompt，
去掉了针对特定主播的部分）。字幕按组送给 LLM（`translate.group`，80 行），前后各带 `translate.context`（5）行上下文；
回复 `{"lines": [{"id", "text"}]}` 一对一对回去，所以时间轴不会跑掉。漏掉的行会再问一次，还是没有就保留日文。
输出 `<stem>.zh.transcript.json`；录播页默认在影片上显示中文字幕，字幕栏可以切换 中 / 日 / 双语，也能下载 SRT。
5 小时的直播大约 12 万 token。手动：`uv run -m backend.translate <video>`。

## 网页

`uv run -m backend.monitor.server`（服务器上的通过 Tailscale 打开，见[部署](#部署)）。四个页面：

- **直播状态**（[monitor.html](frontend/monitor.html)）：谁在直播 / 待机 / 未开播，带封面和标签搜索。
  「＋ 新增主播」加主播；每张卡片的齿轮 ⚙ 可以设定自动下载、保留录播数、自动上传到腾讯文档和目标表格，
  也可以删除主播。开着的设定会在卡片上显示 ⬇ 自动下载 / ⬆ 自动上传。
- **录播**（[archives.html](frontend/archives.html)）：已下载的录播和处理进度。每场都能播放，
  带中文（默认）/ 日文字幕、中 / 日 / 双语字幕栏、弹幕回放和弹幕密度图。可下载：影片、弹幕、
  SRT（中 / 日）、路灯表（Excel），或打包成一个 zip（不含原始弹幕）。
- **路灯表**（[highlights.html](frontend/highlights.html)）：每场的路灯表，可按分数、弹幕热度、
  「只看有切片」筛选，话题日志可以展开。
- **通知**（[notify.html](frontend/notify.html)）：QQ 机器人和 Discord 的状态与测试按钮；每个 QQ 群的名字、
  开关、订阅的主播和通知类型；最近 50 条发送记录。

Excel 路灯表（[sheet.py](backend/sheet.py)）打开就能读：表头冻结；内容摘要和切片标题两栏较宽、自动换行、
靠左对齐，其他栏置中；分数有颜色，有 🏆 的行标黄；最后留一栏空白的「剪辑」给团队填。
手动导入腾讯文档或 Google Sheets 时，这些格式都会保留。

API：`/api/status`、`/api/streamers`（GET / POST / PATCH / DELETE）、`/api/downloads`、
`/api/archives/*`、`/api/highlights/*`、`/api/notify*`（见 [server.py](backend/monitor/server.py)、
[archives.py](backend/monitor/archives.py) 和 [notify_api.py](backend/monitor/notify_api.py)）。

## 上传腾讯文档

[tencent_docs.py](backend/tencent_docs.py) 通过腾讯文档官方的 MCP 接口，把路灯表加成一份现有表格里的新子表，
排在最前面（最新的一场永远是第一个子表）。栏位、文字和格式都和 Excel 下载一样：表头冻结、列宽、
置中 / 靠左、自动换行、分数颜色、🏆 行标黄。

自动上传按主播设定：在齿轮 ⚙ 里勾「自动上传到腾讯文档」，贴上目标表格的链接（每个主播可以用自己的表格，
也可以共用一份）。路灯表做好后就会上传（分析之后的一个阶段：和其他阶段一样会重试，
Discord 会收到 📤 或 ❗，附上子表链接）。同一场再传会覆盖旧的子表。手动：

```sh
uv run -m backend.tencent_docs downloads/<主播>/<YYYYMMDD>/<标题>/ --sheet <链接或 FILE_ID>
uv run -m backend.tencent_docs <.highlights.json> --sheet <链接> --title "20261111 xx"
```

凭证（所有主播共用）：一个个人 MCP token，用拥有（或能编辑）这些表格的 QQ / 微信账号，
到 <https://docs.qq.com/open/auth/mcp.html> 扫码就能拿到，不需要开发者注册，也不需要大陆身份证。
填进 `.env` 的 `TENCENT_DOCS_MCP_TOKEN`；没填的话会跳过上传（只记日志）。token 过期后上传会失败，
Discord 会收到「rejected the token」，换一个新的即可。在服务器上改完后要用
`docker compose up -d --force-recreate kiri` 重启（单纯 restart 不会重新读 `.env`）。

细节：子表名是 `<YYYYMMDD> <主播> <标题>`，截到 31 个字符；重传会先删掉旧的子表，只有它是整份表格
唯一的子表时，新的才会加上 ` (2)`。上传一场约 10 秒，调用接口约 10 次加上每段同色的连续行各一次
（5 小时的直播约 50 次）。免费账号每天 2000 次（官方 MCP 说明页），足够每天传几十场。

## 本地预览

不部署也能检查页面改动：

```sh
uv run scripts/pull_archive.py 鹿乃 --minutes 3    # 从服务器复制一场录播（影片只取 3 分钟）
uv run -m backend.monitor.server --dev --reload    # http://127.0.0.1:8770
```

`--dev` 会显示页面（和直播状态），但不下载、不转写、不分析，也不发 Discord。改 HTML 刷新浏览器就能看到；
`--reload` 会在后端代码改动时重启服务器。`pull_archive.py` 通过 ssh 把原始弹幕以外的所有文件传到
`downloads/`（`--minutes 0`：不要影片，`--full`：完整影片）。

## 测试

```sh
uv run pytest                          # 全部离线测试（约 50 秒）
uv run pytest -m network -s            # 连真实的 YouTube / Twitch 检查
STRESS_SEEDS=1000 uv run pytest tests/test_random_sequences.py   # 更长的压力测试
```

网页的浏览器测试（[test_monitor_page.py](tests/test_monitor_page.py)，Playwright）需要 Chromium
（`uv run playwright install chromium`）；没装就会自动跳过，CI 就是这样（CI 只跑代码测试）。

## 部署

CI（[ci.yml](.github/workflows/ci.yml)）在每个 pull request 和 main 上跑测试并构建 Docker 镜像。
main 上的 CI 通过后，[deploy.yml](.github/workflows/deploy.yml) 通过 SSH 把代码复制到服务器，
执行 `docker compose up -d --build`。网页只在服务器的 localhost 上监听，通过 Tailscale 访问。

```sh
docker compose up -d --build     # 在本地跑容器（需要 .env）
```

服务器的一次性设定（Docker、部署用户、SSH key、GitHub secrets、Tailscale）：[docs/deploy.md](docs/deploy.md)。

## 配置

`config.json` 放所有可以提交的设定：`monitor`、`notify`、`llm`、`analysis`、`translate`、`asr`、
`upload`、`download`。密钥放在 `.env`（见 [.env.example](.env.example)）：LLM key、`DISCORD_WEBHOOK_URL`、
`TWITCH_OAUTH`、`LLM_PROXY` / `LLM_PROXY_TOKEN`、`TENCENT_DOCS_MCP_TOKEN`、`QQ_BOT_APP_ID` / `QQ_BOT_SECRET`。网页上改的东西都存在 `data/`
（主播名单、队列、提醒状态），部署不会动它。

## 授权

本项目采用 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）。可以自由使用、修改和分发；
但修改后的版本如果以网络服务的形式提供给别人使用，也必须公开源代码。
路灯表分析和翻译的 prompt 改编自同为 AGPL-3.0 的 [AutoKiri](https://github.com/anson70242/AutoKiri)。
