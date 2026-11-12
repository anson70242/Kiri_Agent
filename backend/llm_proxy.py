"""Tiny HTTPS proxy that lets the server reach LLM APIs through this machine.

Runs on a machine in a region the LLM providers support; the server sets
`LLM_PROXY=http://kiri:<token>@<this machine's Tailscale IP>:8118` and only its LLM
client uses it. Everything else on the server (downloads, monitor) connects directly.

    uv run -m backend.llm_proxy                 # listen on this machine's Tailscale IP
    uv run -m backend.llm_proxy --host 127.0.0.1 --port 8118
    pythonw -m backend.llm_proxy --log data/llm_proxy.log   # no console (autostart)

Two kinds of traffic:
- HTTPS CONNECT tunnels to port 443 of the hosts in ALLOWED_HOSTS (official APIs;
  extend with LLM_PROXY_ALLOW="host1,host2" in .env).
- Plain HTTP requests to a local LLM gateway on this machine, e.g. CLIProxyAPI on
  127.0.0.1:8317 (LLM_PROXY_HTTP_ALLOW). The server sets base_url to
  http://127.0.0.1:8317/v1 and the request runs from here.
Anything else gets 403.

Safety:
- Needs `LLM_PROXY_TOKEN` (from .env) as the proxy password; refuses to start without it.
- Bind it to the Tailscale address, never 0.0.0.0, so only your tailnet can reach it.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import logging
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

log = logging.getLogger("llm_proxy")
REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOWED_HOSTS = {
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
}
# Local LLM gateway (CLIProxyAPI) the server may call through us as plain HTTP.
# Override with LLM_PROXY_HTTP_ALLOW="127.0.0.1:8317,127.0.0.1:11434" ("" disables).
DEFAULT_HTTP_ALLOW = "127.0.0.1:8317"
HEADER_LIMIT = 16 * 1024
IDLE_TIMEOUT = 15  # seconds to send the CONNECT request


def tailscale_ip() -> str | None:
    """This machine's Tailscale IPv4 (100.64.0.0/10), if any: the local address the OS
    would use to reach Tailscale's DNS at 100.100.100.100 (UDP connect sends nothing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("100.100.100.100", 53))
            ip = s.getsockname()[0]
        except OSError:
            return None
    a, b = (int(x) for x in ip.split(".")[:2])
    return ip if a == 100 and 64 <= b <= 127 else None


class Proxy:
    def __init__(self, token: str, allowed: set[str], upstream_port: int = 443,
                 http_allowed: set[str] | None = None):
        self.token = token
        self.allowed = allowed
        self.upstream_port = upstream_port  # tests point this at a local server
        # "host:port" of plain-HTTP services on this machine the server may reach,
        # e.g. a local LLM gateway on 127.0.0.1:8317.
        self.http_allowed = http_allowed or set()

    def authorized(self, header: str | None) -> bool:
        if not header:
            return False
        # Accept any user name; only the password (token) matters.
        try:
            scheme, value = header.split(" ", 1)
            _, password = base64.b64decode(value).decode().split(":", 1)
        except ValueError:
            return False
        return scheme.lower() == "basic" and hmac.compare_digest(password, self.token)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            return await self.reply(writer, 400, "Bad Request")
        headers = {
            k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)
        }
        if not self.authorized(headers.get("proxy-authorization")):
            log.warning("%s: rejected (bad or missing password)", peer)
            return await self.reply(writer, 407, "Proxy Authentication Required",
                                    'Proxy-Authenticate: Basic realm="kiri"\r\n')
        if method.upper() != "CONNECT":
            return await self.forward(reader, writer, peer, lines, method, target)
        host, _, port = target.rpartition(":")
        host = host.strip("[]").lower()
        if port != "443" or host not in self.allowed:
            log.warning("%s: refused %s", peer, target)
            return await self.reply(writer, 403, "Forbidden")

        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, self.upstream_port), 15)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("%s: can't reach %s: %s", peer, host, e)
            return await self.reply(writer, 502, "Bad Gateway")
        log.info("%s -> %s", peer, host)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    async def forward(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: object,
                      lines: list[str], method: str, target: str) -> None:
        """Plain-HTTP forward proxy (absolute-form request), only to `http_allowed`.
        One request per connection: the upstream is told `Connection: close`."""
        url = urlsplit(target)
        try:
            host, port = (url.hostname or "").lower(), url.port or 80
        except ValueError:
            return await self.reply(writer, 400, "Bad Request")
        if url.scheme != "http" or f"{host}:{port}" not in self.http_allowed:
            log.warning("%s: refused %s %s", peer, method, target)
            return await self.reply(writer, 403, "Forbidden")
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("%s: can't reach %s:%s: %s", peer, host, port, e)
            return await self.reply(writer, 502, "Bad Gateway")
        log.info("%s -> http://%s:%s%s", peer, host, port, url.path)
        version = lines[0].rsplit(" ", 1)[-1]
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        hop = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}
        kept = [ln for ln in lines[1:] if ln and ln.split(":", 1)[0].strip().lower() not in hop]
        up_writer.write("\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""]).encode("latin-1"))
        await up_writer.drain()
        to_upstream = asyncio.create_task(pipe(reader, up_writer))  # request body
        await pipe(up_reader, writer)  # response, until the upstream closes
        to_upstream.cancel()

    @staticmethod
    async def reply(writer: asyncio.StreamWriter, code: int, text: str, extra: str = "") -> None:
        writer.write(f"HTTP/1.1 {code} {text}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
        try:
            await writer.drain()
        finally:
            writer.close()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def serve(host: str, port: int, proxy: Proxy) -> None:
    server = await asyncio.start_server(proxy.handle, host, port, limit=HEADER_LIMIT)
    log.info("LLM proxy on %s:%d, allowed: %s", host, port, ", ".join(sorted(proxy.allowed)))
    async with server:
        await server.serve_forever()


def main() -> int:
    p = argparse.ArgumentParser(description="HTTPS proxy for LLM API calls from the server.")
    p.add_argument("--host", help="address to listen on (default: this machine's Tailscale IP)")
    p.add_argument("--port", type=int, default=8118)
    p.add_argument("--log", type=Path, help="log to this file instead of the console")
    args = p.parse_args()
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if args.log:
        log_path = args.log if args.log.is_absolute() else REPO_ROOT / args.log
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format=fmt, filename=log_path, encoding="utf-8")
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)

    load_dotenv(REPO_ROOT / ".env")
    token = os.getenv("LLM_PROXY_TOKEN", "").strip()
    if len(token) < 16:
        log.error("Set LLM_PROXY_TOKEN (16+ random chars) in .env first.")
        return 1
    host = args.host
    while not host:  # at logon Tailscale may come up after us: wait for it
        if host := tailscale_ip():
            break
        log.warning("no Tailscale IPv4 yet; retrying in 15 s (or pass --host)")
        time.sleep(15)
    extra = {h.strip().lower() for h in os.getenv("LLM_PROXY_ALLOW", "").split(",") if h.strip()}
    http_allowed = {h.strip().lower() for h in os.getenv("LLM_PROXY_HTTP_ALLOW", DEFAULT_HTTP_ALLOW).split(",") if h.strip()}
    if http_allowed:
        log.info("plain HTTP forwarding to: %s", ", ".join(sorted(http_allowed)))
    try:
        asyncio.run(serve(host, args.port, Proxy(token, ALLOWED_HOSTS | extra, http_allowed=http_allowed)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
