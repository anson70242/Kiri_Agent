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

重启之前，部署会先用新版本的代码把服务器上的状态文件读一遍（只读，不改任何文件）。出现下面任一情况，部署就停在这一步，服务器继续跑旧版本：

- 有状态文件新版本读不了：名单、用户、QQ 群订阅会让新版本拒绝启动；队列、直播状态、通知记录、登录会话会被新版本备份后清空。日志里会写出是哪个文件和原因。
- 下载队列里还有任务。等处理完，再到 Actions → Deploy → Run workflow 重新部署；急着部署就勾上 `force`，只跳过这一项检查。

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
