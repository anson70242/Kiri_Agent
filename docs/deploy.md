# 部署（Docker + GitHub Actions + Tailscale）

```
git push main ─▶ CI（测试 + 构建镜像）─通过─▶ Deploy：rsync 代码到服务器 ─▶ docker compose up --build
                                                                               │
你的电脑 / 手机 ──Tailscale（私人网络）──▶ tailscale serve ──▶ 127.0.0.1:8770（容器）
```

- PR 和 push 到 main 都会跑 CI（[ci.yml](../.github/workflows/ci.yml)）。
- 只有 main 上 CI 通过的那个 commit 才会部署（[deploy.yml](../.github/workflows/deploy.yml)）。
- 服务器上的 `.env`（密钥）和数据卷 `kiri_kiri-data`（通知状态）部署时不会被动到。
- 网页只监听服务器本机，公网 IP 上看不到；你通过 Tailscale 访问。

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

## 部署

push 到 main 后，CI 通过就会自动部署。
也可以在 GitHub → Actions → Deploy → Run workflow 手动部署当前的 main。

## 日常操作（在服务器上）

```sh
cd ~/kiri
docker compose ps              # 状态（healthy = 正常）
docker compose logs -f         # 看日志
docker compose restart         # 重启
```

- **改主播名单**：在本地改 `config.json`，push 到 main，会自动部署。
- **改密钥**：改服务器上的 `~/kiri/.env`，然后跑 `docker compose up -d`。
- 服务器上的服务和你本地跑的 `uv run -m backend.monitor.server` 会各自发 Discord 通知。服务器部署好以后，本地就不要再开着监控了，否则会收到两份通知。

## 从零开始装一台新服务器

1. 装 Docker（`curl -fsSL https://get.docker.com | sudo sh`）和 rsync，把部署用户加进 `docker` 组。
2. 用 `ssh-keygen -t ed25519 -N ""` 生成一把专用密钥，公钥加到部署用户的 `~/.ssh/authorized_keys`，
   前面加上 `no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty `。
3. 建部署目录，放好 `.env`（权限 600）。
4. 按上面的表格填 GitHub secrets，`DEPLOY_PATH` 设成部署目录。
5. 装 Tailscale 并跑 `tailscale serve --bg 8770`。
