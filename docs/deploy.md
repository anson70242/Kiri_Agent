# 部署（Docker + GitHub Actions + Tailscale）

```
git push main ─▶ CI（测试 + 构建镜像）─通过─▶ Deploy：rsync 代码到服务器 ─▶ docker compose up --build
                                                                               │
你的电脑 / 手机 ──Tailscale（私人网络）──▶ tailscale serve ──▶ 127.0.0.1:8770（容器）
```

- PR 和 push 到 main 都会跑 CI（[ci.yml](../.github/workflows/ci.yml)）。
- 只有 main 上 CI 通过的那个 commit 才会部署（[deploy.yml](../.github/workflows/deploy.yml)）。
- 服务器上的 `.env`（密钥）和数据卷 `kiri_kiri-data`（通知状态）部署时不会被动到。
- 网页本身只监听服务器本机；你通过 Tailscale 访问。要从公网 IP 用 HTTPS 访问，见「公网 HTTPS」。

## 服务器

| | |
| --- | --- |
| 机器 | `HK-Server`（<server-ip>，Ubuntu 24.04），本机 `~/.ssh/config` 里有 |
| 部署用户 | `anson`（在 `docker` 组） |
| 部署目录 | `/home/anson/kiri`（workflow 的默认值；要换就设 GitHub Variable `DEPLOY_PATH`） |
| 密钥 | `/home/anson/kiri/.env`，权限 600，目前只有 `DISCORD_WEBHOOK_URL` |
| 部署钥匙 | `anson` 的 `authorized_keys` 里注释为 `github-actions-deploy@kiri` 的那行；限制成不能开终端、不能转发端口 |

撤销部署权限：把 `authorized_keys` 里 `github-actions-deploy@kiri` 那行删掉。

## GitHub secrets

仓库 → Settings → Environments → New environment，名字填 `production`，在里面加：

| 名称 | 内容 |
| --- | --- |
| `DEPLOY_HOST` | `<server-ip>` |
| `DEPLOY_USER` | `anson` |
| `DEPLOY_SSH_KEY` | 部署私钥的完整内容，包括 BEGIN / END 两行 |
| `DEPLOY_KNOWN_HOSTS` | `ssh-keyscan <server-ip>` 的输出（服务器指纹） |
| `DEPLOY_PORT` | 可选，SSH 端口，不填就是 22 |

## Tailscale：只让自己看网页

服务器（要 sudo）：

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                 # 会给一个网址，打开后用你的账号登录
sudo tailscale serve --bg 8770    # 把网页分享给你的 Tailscale 设备
```

第一次跑 `tailscale serve` 时，如果提示要开启 HTTPS，就按它给的链接去 Tailscale 后台开启，再跑一次。
成功后会显示一个网址，像 `https://<机器名>.<你的tailnet>.ts.net`。这个设置重启后会保留。

你的电脑和手机：装 Tailscale App，用同一个账号登录，打开上面的网址就能看。

## 公网 HTTPS（没有域名，直接用 IP）

不装 Tailscale 的人也要能看时，用 `caddy` 服务把网页挂到服务器的公网 IP 上：
`https://<公网IP>`。证书是 Let's Encrypt 免费的 IP 证书（有效期约 6 天，`shortlived` 档），
Caddy 自己申请、自己续期，配置在 [caddy/Caddyfile](../caddy/Caddyfile)。
Tailscale 那条路照旧能用。

```
浏览器 ──https://<公网IP>──▶ 路由器 80/443 转发 ──▶ caddy（容器）──▶ kiri:8770
```

**一定要先有登录。** 公开后任何人都能打开这个网址，所以要等 kiri 的登录功能上线后再开。

**1. 开端口**

- 路由器（服务器在内网 `192.168.x.x` 后面）：把 TCP **80** 和 **443** 转发到服务器的内网 IP；
  在路由器里给服务器固定这个内网 IP（DHCP 保留）。
- 服务器防火墙如果开着：`sudo ufw allow 80,443/tcp`。云主机则在安全组里放行 80、443。
- 80 也必须开：Let's Encrypt 从外网连 80（http-01）或 443（tls-alpn-01）来验证这个 IP，续期也是。

**2. 在服务器 `~/kiri/.env` 加三行**，然后 `docker compose up -d`：

```sh
COMPOSE_PROFILES=public       # 打开 caddy 服务；删掉这行就关掉
PUBLIC_IP=<公网IPv4>          # curl -4 ifconfig.me 查；IP 不要写进 git（仓库是公开的）
PUBLIC_BIND_IP=<服务器内网IP>  # ip -4 addr 查，例如 192.168.1.10
```

`PUBLIC_BIND_IP` 不能省：`tailscale serve` 已经占了 Tailscale IP 上的 443，
caddy 再去占 `0.0.0.0:443` 会报 `address already in use`，所以只绑内网 IP。

**3. 检查证书**

```sh
docker compose logs caddy | grep -iE "certificate obtained|error"   # 看到 certificate obtained 就成功
curl -sv https://<公网IP> -o /dev/null 2>&1 | grep -E "subject|expire|issuer"
```

在外网（例如手机关掉 Wi-Fi）打开 `https://<公网IP>`，浏览器不应有警告。

**续期**：证书约 6 天过期，Caddy 在后台按 Let's Encrypt 建议的时间（过期前两三天）自动续，不用管。
证书存在数据卷 `kiri_caddy-data` 里，重新部署不会丢、也不会重新申请。
续期要求 80/443 一直能从外网连到；端口转发被关掉的话，几天后网页就会出现证书错误。

**公网 IP 变了**：家用宽带的 IP 可能会变。变了就改 `.env` 的 `PUBLIC_IP`，再
`docker compose up -d --force-recreate caddy`，Caddy 会为新 IP 申请证书。

**关掉公网访问**：删掉 `.env` 里的 `COMPOSE_PROFILES=public`，然后 `docker compose stop caddy && docker compose rm -f caddy`。

## 部署

push 到 main 后，CI 通过就会自动部署。
也可以在 GitHub → Actions → Deploy → Run workflow 手动部署当前的 main。

只改文档的 push（`*.md`、`docs/`、`LICENSE`）不跑 CI，也就不部署、不重启。
`GLOSSARY.md` 例外：测试会读它，所以改它照常跑 CI 并部署。

重启之前，部署会先用新版本的代码把服务器上的状态文件读一遍（只读，不改任何文件）。出现下面任一情况，部署就停在这一步，服务器继续跑旧版本：

- 有状态文件新版本读不了：名单、用户、QQ 群订阅会让新版本拒绝启动；队列、直播状态、通知记录、登录会话、群文件的任务和上传记录会被新版本备份后清空。日志里会写出是哪个文件和原因。
- 下载队列里还有任务，或有一场录播或一个群文件正在上传到网盘（只是排队、等弹幕、等重试或等重新授权的网盘上传不算，正在下载的群文件也不算）。等处理完，再到 Actions → Deploy → Run workflow 重新部署；急着部署就勾上 `force`，只跳过这一项检查，正在上传的那场（或那个群文件）会在重启后从头再传。

想在服务器上手动检查：`docker compose run --rm --no-deps -T kiri /app/.venv/bin/python -m backend.check_state`。

合并之前想先看 CI 结果：把分支 push 上去开一个 draft PR。CI 会跑，不会部署。

## 日常操作（在服务器上）

```sh
cd ~/kiri
docker compose ps              # 状态（healthy = 正常）
docker compose logs -f         # 看日志
docker compose restart         # 重启
```

- **改主播名单**：在本地改 `config.json`，push 到 main，会自动部署。
- **改密钥**：改服务器上的 `~/kiri/.env`，然后跑 `docker compose up -d`。
- **下载影片**：`docker compose exec kiri /app/.venv/bin/python -m backend.downloader <url>`。
  文件在数据卷 `kiri_kiri-downloads` 里（容器内 `/app/downloads`），
  拿出来用 `docker compose cp kiri:/app/downloads/<主播> ./`。
- 服务器上的服务和你本地跑的 `uv run -m backend.monitor.server` 会各自发 Discord 通知。服务器部署好以后，本地就不要再开着监控了，否则会收到两份通知。

## YouTube 会限 cookie

服务器没有浏览器，所以会限影片靠一份 cookie 文件：容器里的 `/app/data/youtube_cookies.txt`
（数据卷 `kiri_kiri-data`，部署不会动它）。普通下载失败时，下载器会带上这份 cookie 重试一次。

**1. 本机导出（Firefox）**

1. 装扩展 [cookies.txt](https://addons.mozilla.org/firefox/addon/cookies-txt/)，并在扩展设置里允许它在无痕窗口运行。
2. 开一个新的无痕窗口（`Ctrl+Shift+P`），登录有会员资格的 YouTube 账号。
3. 在同一个分页打开 `https://www.youtube.com/robots.txt`（只开这一个无痕分页）。
4. 用扩展导出当前网站的 cookie，存成 `youtube_cookies.txt`。
5. 关掉无痕窗口，之后别再用这个会话登录 YouTube，否则 YouTube 会轮换 cookie，导出的就失效了。

**2. 传到服务器（本机 PowerShell）**

```sh
scp youtube_cookies.txt HK-Server:~/
```

**3. 放进容器（服务器上）**

```sh
cd ~/kiri
docker compose cp ~/youtube_cookies.txt kiri:/app/data/youtube_cookies.txt
docker compose exec -u root kiri sh -c 'chown app /app/data/youtube_cookies.txt && chmod 600 /app/data/youtube_cookies.txt'
rm ~/youtube_cookies.txt        # 别在家目录留副本
```

`chown` 不能省：yt-dlp 会把更新后的 cookie 写回这个文件，文件要归容器里的 `app` 用户才写得进去。

**4. 测试**

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.downloader --info <会限影片链接>
```

能印出标题就是成功。日志里出现 `retrying with cookies file youtube_cookies.txt` 表示用到了这份 cookie。

**失效了怎么办**：报错里有 `Sign in`、`members-only` 或 `This content isn't available`，就照上面重新导出一份，覆盖旧文件。

- 这份 cookie 等于账号登录态，不要提交到 git，也不要发给别人。
- 用账号跑 yt-dlp 有被 YouTube 封号的风险，下载频率别太高；能接受的话用小号开会员。

## TwitCasting 回放要登录 cookie

TwitCasting 的回放一律要登录才能看，所以下载 TwitCasting 回放**每次都带**一份单独的 cookie 文件：
容器里的 `/app/data/twitcasting_cookies.txt`（设置 `download.twitcasting_cookies_file`，同一个数据卷 `kiri_kiri-data`，部署不会动它）。
它和 YouTube 的 cookie 文件互不相干，换一份不会影响另一份；TwitCasting 也不会用 YouTube 的 cookie 或浏览器的 cookie。
第一次自动下载 TwitCasting 之前就要放好；没有这份文件，下载会以「登录缺失」失败。

**1. 本机导出（Firefox）**：跟上面 YouTube 一样用 cookies.txt 扩展，在新的无痕窗口登录 TwitCasting 账号，
打开 `https://twitcasting.tv/`，导出当前网站的 cookie，存成 `twitcasting_cookies.txt`，然后关掉无痕窗口。

**2. 传到服务器并放进容器**

```sh
scp twitcasting_cookies.txt HK-Server:~/          # 本机 PowerShell
cd ~/kiri                                         # 以下在服务器上
docker compose cp ~/twitcasting_cookies.txt kiri:/app/data/twitcasting_cookies.txt
docker compose exec -u root kiri sh -c 'chown app /app/data/twitcasting_cookies.txt && chmod 600 /app/data/twitcasting_cookies.txt'
rm ~/twitcasting_cookies.txt
```

`chown` 同样不能省：yt-dlp 会把更新后的 cookie 写回这个文件。

**3. 测试**（找一个已公开的回放，网址形如 `twitcasting.tv/<ID>/movie/<数字>`）

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.downloader --info <回放网址>
```

能印出标题就是成功。报错有 `login missing or expired` 表示 cookie 没带上或已失效。
**还没验证**服务器的 IP 能不能用本机导出的 cookie（换地方登录可能让会话失效），第一次部署后马上用这条命令确认。

**失效了怎么办**：自动下载放弃时，失败通知会写「可能是 TwitCasting 的登录 cookies 过期：更新服务器上的 `data/twitcasting_cookies.txt`」。
照上面重新导出一份，覆盖旧文件即可，不用重启服务；在修好之前 TwitCasting 回放都下载不了。

## 夸克网盘（网盘副本）

主播的齿轮里勾了「自动上传到夸克网盘」，每场录播处理完，程序就把它的网盘副本（一个 zip：影片、弹幕、中日字幕 SRT、路灯表 Excel）
传到夸克网盘，并做一个网盘链接（带提取码、永久有效），放进「全部处理完成」和录播页的「☁ 网盘链接」。
上传靠夸克官方的命令行工具（CLI，`quarkclouddrive` 1.0.25，一个 Node 程序）；为什么用它、要承担什么风险，见
[ADR 0001](adr/0001-quark-drive-through-the-official-agent-cli.md)（英文）。

- **CLI 不在仓库里，也不在镜像里**（仓库是公开的）。镜像只带 Node；CLI 要你手动放进数据卷 `kiri_kiri-data`，
  容器内的位置是 `/app/data/quark/quarkclouddrive-1.0.25/`（`config.json` 的 `drive.quark_cli` 指向它的 `scripts/quark-drive.cjs`，默认就是这里；
  设成空字符串就是不用网盘）。服务启动时日志里会有一行 `drive available` 或 `drive not available`。
- **授权存在 CLI 自己的文件夹里**：`quarkclouddrive-1.0.25/claudecode/`（token、设备 ID、上传记录）。
  所以授权跟着数据卷走，部署、重建容器都不会丢。这个文件夹等于网盘账号的登录态：不要提交到 git，也不要发给别人。
- **容器的主机名固定是 `kiri`**（`docker-compose.yml` 的 `hostname`，不要改）。第一次授权时，夸克认的设备 ID 是从主机名算出来的，
  之后就存在 `claudecode/` 里；主机名不变，以后重新授权也还是同一台设备。
- **还没放 CLI** 时一切照旧：勾了网盘的主播只在日志里留一条警告，「全部处理完成」照常发、用网站自己的下载链接。
  **放了 CLI 但还没授权**时，第一场要上传的录播会发「网盘授权失效」，照下面第 2 步授权即可。
- **永远不要跑 CLI 自带的 `install.sh` 和 `uninstall.sh`**：`install.sh` 会联网下载别的版本覆盖它（还会自己装 Node），
  `uninstall.sh` 会撤销授权、删掉 `claudecode/`。所以第 1 步放进去时就把这两个文件删掉。程序也从不更新 CLI。

下面的命令都在服务器的 `~/kiri` 里跑。

**1. 把 CLI 放进数据卷**

CLI 来自夸克官方的 skill 包，文件夹名是 `quarkclouddrive-1.0.25`（里面有 `SKILL.md`、`scripts/quark-drive.cjs`）。
固定用这个版本：程序是照 1.0.25 的输出写和测试的。在仓库外面留一份（例如本机 `reference/`，它被 git 忽略）。

```sh
scp -r quarkclouddrive-1.0.25 HK-Server:~/        # 本机 PowerShell，在放这个文件夹的目录里跑
```

```sh
cd ~/kiri                                         # 以下在服务器上
docker compose exec kiri mkdir -p /app/data/quark
docker compose cp ~/quarkclouddrive-1.0.25 kiri:/app/data/quark/
docker compose exec -u root kiri sh -c 'rm -f /app/data/quark/quarkclouddrive-1.0.25/scripts/install.sh /app/data/quark/quarkclouddrive-1.0.25/scripts/uninstall.sh && chown -R app:app /app/data/quark'
rm -r ~/quarkclouddrive-1.0.25
docker compose exec kiri node --version           # 要 v16 以上
```

`chown` 不能省：`docker compose cp` 放进去的文件归 root，而 CLI 要在自己的文件夹里建 `claudecode/` 写授权，得归容器里的 `app` 用户才写得进去。

**2. 授权**

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.quark login
```

它会印出一个授权链接（里面带设备 ID，只给自己看）。在手机或电脑的浏览器里打开，用要存录播的那个夸克账号登录并同意授权，页面会给你一个授权码。
链接只在几分钟内有效，过期了就再跑一次 `login` 拿新的。拿到授权码马上贴回来：

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.quark login --code <授权码>
```

印出「授权成功」就好了；「授权失败：…」时照原因重来（多半是授权码过期，重新 `login`）。
已经授权过时 `login` 只印「已授权，不用再授权」。授权码等于一次登录：不要把它贴到聊天、issue 或任何文件里。

已经有一份授权好的 `claudecode/`（例如之前在另一个主机名也是 `kiri` 的容器里授权过）时，可以不重新授权：
把那个 `claudecode` 文件夹照第 1 步的方式 `docker compose cp` 到 `/app/data/quark/quarkclouddrive-1.0.25/` 里，
再跑一次第 1 步的 `chown`，然后用第 3 步检查。之后就别再用原来那份了。

**3. 检查**

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.quark status
```

印出「已授权」、会员类型和到期日（`会员：SVIP（到期 YYYY-MM-DD）`）、已用 / 总空间就是成功（不会印出账号昵称）。
没授权或授权失效时印「未授权或授权已失效：…」和要跑的命令，照第 2 步重新授权。
（三条命令出错时退出码是 1；没找到 CLI 时印「夸克网盘不可用」，退出码 2。）之后就可以在齿轮里给主播勾「自动上传到夸克网盘」了；
网盘文件夹从网盘根目录算起，留空就是 `录播/<主播名>`（主播名和服务器上录播文件夹的名字一样，文件夹名不能有的字符换成 `_`），不存在会自动建。

**4. 手动上传以前的录播**

勾选之前就处理完的录播不会自动上传。还在服务器上的，可以手动传：

```sh
docker compose exec kiri /app/.venv/bin/python -m backend.quark upload "/app/downloads/<主播>/<YYYYMMDD>/<标题>"
docker compose exec kiri /app/.venv/bin/python -m backend.quark upload "/app/downloads/<主播>/<YYYYMMDD>" --folder 直播录像/某主播
```

第一行传一场录播，第二行传一个文件夹里的每一场（`--folder` 指定网盘文件夹）。不写 `--folder` 时，用名单里同名主播齿轮里的文件夹，
没有就是 `录播/<主播>`。每场印一行结果，后面一行是它的网盘链接（并记进那场录播，录播页会出现「☁ 网盘链接」）：

```
已上传：[20261118][某主播] 标题.zip → 录播/某主播/[20261118][某主播] 标题.zip
  网盘链接：https://pan.quark.cn/s/…?pwd=…
已在网盘：…        （网盘文件夹里已有同名的文件：不再打包、不再上传；这场还没有网盘链接时会补做）
跳过：某主播/20261119/标题（没有影片）     （或「还在下载」）
失败：….zip：<夸克的原话>
```

分享没做成时第二行是「没有网盘链接：<原因>」（zip 已经在网盘上了）。所以跑两次也没关系。
授权失效时印出要跑的命令并停下，剩下的不传。有失败时退出码是 1。

- 很慢：实测约 1.2–2 MB/s，10 GB 的录播要一个半到两个半小时。传大的录播时在 `tmux` 里跑，免得 SSH 断线把它中断。
- 部署检查看不到手动上传：这时部署（或 `docker compose restart`）会把它打断，打断的那场在网盘上不会留下文件。部署完再跑一次同一条命令就行。
- 已经被保留数量删掉的录播传不了。

**5. 收到「网盘授权失效」时**

夸克不再接受这份授权了（过期、在夸克 App 里被撤销，或还没授权过）。这时网盘上传都在等待，什么都不会丢：

1. 照第 2 步重新授权（`login`，打开链接拿授权码，`login --code`），再用第 3 步的 `status` 确认。
2. 不用重启。等待中的上传会在下一次重试时（每 15 分钟，`download.auto.retry_minutes`）自己继续，传完照常发「网盘上传完成」和网盘链接。
3. 失效后 72 小时内没重新授权，那几场会以「网盘上传失败」放弃；放弃的录播之后会按保留数量照常清理，
   所以要在它被删掉之前用第 4 步手动传。

一份授权能用多久没有确定：实测授权里的长期 token 写着授权后 180 天到期，但 CLI 自动刷新 token 时这个日期会不会往后推，还不知道（还在观察）。
不要指望它；失效时会收到「网盘授权失效」。

**6. 手动换成新版本的 CLI**

程序不会自己更新 CLI，只有确实需要（例如夸克不再接受 1.0.25）时才手动换。新版本的命令或输出可能变了，换完一定要检查。

1. 等到没有正在上传的录播和群文件（录播页没有「网盘上传 xx%」）。
2. 照第 1 步把新版本的文件夹（例如 `quarkclouddrive-1.0.26`）放进 `/app/data/quark/`，同样删掉 `install.sh` / `uninstall.sh`。
3. 把旧版本的授权（`claudecode` 文件夹）复制过去，旧文件夹先留着：
   `docker compose exec kiri cp -a /app/data/quark/quarkclouddrive-1.0.25/claudecode /app/data/quark/quarkclouddrive-1.0.26/`
4. 在本地把 `config.json` 的 `drive.quark_cli` 改成新文件夹里的 `scripts/quark-drive.cjs`，push 到 main，等自动部署。
5. 用第 3 步的 `status` 检查，再用第 4 步手动传一场小的录播试试。都正常后，删掉旧文件夹（直接 `rm -r`，**不要**跑它的 `uninstall.sh`，那会撤销授权）。
   不正常就把 `config.json` 的路径改回旧版本，再部署一次。

## 群文件上传到夸克网盘

QQ 群里有人发了文件（在聊天里发，或传到群文件面板，机器人分不出这两种），文件名里有下面某个标记、扩展名也对，程序就把它传到夸克网盘上这个群的文件夹。
别的文件一概不管。用的是上一节的同一个夸克账号和 CLI；`.env` 和 `config.json` 都不用改。
为什么从机器人收到的消息里拿群文件、因此拿不到什么，见 [ADR 0002](adr/0002-group-files-through-the-official-bots-message-events.md)（英文）。

| 文件名里有 | 扩展名 | 网盘文件夹 |
| --- | --- | --- |
| 【待轴】 | .mp4 | `<群名>/剪辑/` |
| 【空轴】 | .ass | `<群名>/空轴/` |
| 【待校】 | .ass | `<群名>/待校/` |
| 【已校】 | .ass | `<群名>/已校/` |
| 【熟肉】 | .mp4 | `<群名>/熟肉/` |

`<群名>` 是通知页上给这个群起的名字，放在网盘根目录；没有的文件夹会自动建。标记在名字的哪里都行，扩展名不分大小写。

**要打开的**

1. 网盘照上一节放好 CLI、授权。没有时通知页的群文件那一栏会写「这台服务器上没有夸克网盘」。
2. 群主在手机 QQ 里给机器人打开「接收所有消息」（群成员列表里点机器人）。不开的话机器人只收到 @ 它的消息，看不到群文件。
   打开后 QQ 会把群里每条消息都发给机器人；程序只处理附带的文件和 @ 机器人的消息，其他内容不存也不写进日志。
3. 通知页上这个群：先起名字（要能当文件夹名；不行时保存会被拒绝，并说是哪个字符），勾「群文件上传到夸克网盘」，保存。
   想让群里收到上传成功、失败的消息，再勾「群文件上传通知」。两个开关默认都关着，和「开启通知」、订阅的主播互不相干。

在群里 @机器人 加 `帮助` 会列出命令，开了群文件上传的群还会列出上面五条规则；加 `订阅` 显示本群的订阅（群主和管理员才回）。

**传的时候**

- 文件一发出来就下载到服务器（数据卷 `kiri_kiri-downloads` 里的 `.group_files/`），传上网盘后从服务器删掉。
- 同一个文件再发一次不会重复上传。同名但内容不同的文件另存一份，名字里加上发出的时间（UTC+8），例如 `【待校】第3话.2026-11-19-0541.ass`。
- 网盘上只加不删：程序不替换、不移动、不删除网盘上的任何东西，群里的东西也不碰。
- 整个服务同时只传一个文件到网盘：群文件和录播的网盘副本轮流传，两边都在等时群文件先。正在传一场录播时，群文件要等它传完（10 GB 的录播要一个半到两个半小时）。
- 失败会照自动下载的间隔和次数重试（`download.auto.retry_minutes`、`max_errors`）。网盘授权失效时照上一节第 5 步重新授权，等着的群文件会自己接着传；72 小时内没重新授权就放弃。
- 开了「群文件上传通知」的群会收到成功和失败的消息；失败不管开没开都会发到 Discord。
- 群成员还是从 QQ 下载。网盘那边要给谁看，由你在夸克里手动分享群文件夹，程序不做网盘链接。

**拿不到的**

- 群里原有的文件（打开开关之前发的）。
- 服务或机器人掉线时发的文件，包括部署重启的那一分钟里发的：QQ 不会补发，也不会有任何提示。要的话请人重发一次。
- 群文件面板里选的文件夹：程序只看文件名。

**部署**

部署只等正在上传的群文件；排队、等重试和正在下载的不挡（下载会在重启后接着下或重新下）。
强制部署打断的那个，重启后会用服务器上的那份重新上传。部署检查会多印一行，挡住部署时还会写出是哪个群的哪个文件：

```
群文件：3 个任务，1 个正在上传（只有正在上传的会挡住部署）。
正在上传群文件到网盘：某字幕组 群的 【熟肉】第3话.mp4，等它传完再部署。
```

**还没验证的**

- 夸克收不收文件名里的【】（方括号、空格和日文实测可以）。第一次部署后，每条规则发一个真文件试一下。
- QQ 给的下载地址能用多久（至少 30 分钟；程序在文件发出时就下载），机器人能下载的文件有没有大小限制（560 MB 实测可以）。

## LLM 代理（经日本电脑调用 OpenAI / Claude / Gemini）

这些 API 不对香港开放。服务器只让 LLM 请求经 Tailscale 走日本那台电脑出去，
下载、监控仍从香港直连。

```
容器 ──LLM 请求──▶ Tailscale ──▶ 日本电脑 100.126.34.25:8118（backend.llm_proxy）──▶ api.openai.com 等
```

代理只接受带密码的请求，只放行两种：
- HTTPS 隧道到 `api.openai.com`、`api.anthropic.com`、`generativelanguage.googleapis.com`
  （其他域名在电脑 `.env` 里用 `LLM_PROXY_ALLOW=a.com,b.com` 加）。
- 普通 HTTP 转发到电脑本机的 LLM 网关，默认 `127.0.0.1:8317`（CLIProxyAPI / Google agy；
  用 `LLM_PROXY_HTTP_ALLOW=127.0.0.1:8317,...` 改）。

是否走代理由 `config.json` 的 `llm.proxy` 决定（`true` 走、`false` 直连，给不需要代理的人用）。
现在的设定：`base_url` 是 `http://127.0.0.1:8317/v1`，`proxy: true`，
所以服务器的请求经代理到日本电脑，再由电脑转给它自己的 8317。

**日本电脑（一次）**

1. `.env` 里有 `LLM_PROXY_TOKEN=<随机密码>`（已生成）。
2. PowerShell 跑 `.\scripts\install-llm-proxy.ps1`：登录 Windows 就在后台启动，挂了每分钟重启。日志在 `data/llm_proxy.log`。
3. 电脑要一直开着：设置 → 系统 → 电源 → 睡眠设为「从不」。关机或睡眠时服务器的 LLM 调用会失败。

**服务器**

在 `~/kiri/.env` 加两行（密码是电脑 `.env` 里的 `LLM_PROXY_TOKEN`，key 是 agy 的 key，
即电脑 `.env` 里的 `AGY_API_KEY`），然后 `docker compose up -d`：

```sh
LLM_PROXY=http://kiri:<LLM_PROXY_TOKEN>@100.126.34.25:8118
AGY_API_KEY=<agy 的 key>
```

改了 `backend/llm_proxy.py` 之后，电脑上要重启代理：
`Stop-ScheduledTask "Kiri LLM Proxy"; Start-ScheduledTask "Kiri LLM Proxy"`。

测试（不带密码应返回 `407`，说明网络通、代理在跑）：

```sh
curl -s -o /dev/null -w "%{http_connect}\n" -x http://100.126.34.25:8118 https://api.openai.com/
```

## 从零开始装一台新服务器

1. 装 Docker（`curl -fsSL https://get.docker.com | sudo sh`）和 rsync，把部署用户加进 `docker` 组。
2. 用 `ssh-keygen -t ed25519 -N ""` 生成一把专用密钥，公钥加到部署用户的 `~/.ssh/authorized_keys`，
   前面加上 `no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty `。
3. 建部署目录，放好 `.env`（权限 600）。
4. 按上面的表格填 GitHub secrets，`DEPLOY_PATH` 设成部署目录。
5. 装 Tailscale 并跑 `tailscale serve --bg 8770`。
6. 第一次部署后，要用夸克网盘就照「夸克网盘（网盘副本）」放 CLI、授权；要传群文件，再照「群文件上传到夸克网盘」打开。
