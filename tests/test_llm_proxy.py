"""LLM proxy: password, host allowlist, and an actual tunnel to a local echo server."""

import asyncio
import base64
import json

import httpx
import pytest

from backend.llm_proxy import Proxy

TOKEN = "s3cret-token-1234567"


def auth(password: str = TOKEN) -> str:
    return "Proxy-Authorization: Basic " + base64.b64encode(f"kiri:{password}".encode()).decode() + "\r\n"


async def roundtrip(request: str, payload: bytes = b"") -> tuple[str, bytes]:
    async def echo(r, w):
        while data := await r.read(1024):
            w.write(data[::-1])
            await w.drain()
        w.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    proxy = Proxy(TOKEN, {"localhost"}, upstream_port=up_port)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with upstream, server:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(request.encode())
        await w.drain()
        status = (await r.readuntil(b"\r\n\r\n")).decode().split("\r\n")[0]
        echoed = b""
        if payload and " 200 " in status:
            w.write(payload)
            await w.drain()
            echoed = await asyncio.wait_for(r.readexactly(len(payload)), 5)
        w.close()
        return status, echoed


def run(request: str, payload: bytes = b"") -> tuple[str, bytes]:
    return asyncio.run(roundtrip(request, payload))


def test_tunnel_to_allowed_host():
    status, echoed = run(f"CONNECT localhost:443 HTTP/1.1\r\n{auth()}\r\n", b"hello")
    assert " 200 " in status and echoed == b"olleh"


@pytest.mark.parametrize("header", ["", auth("wrong-password-xxxxx"), "Proxy-Authorization: Basic !!!\r\n"])
def test_needs_password(header):
    assert " 407 " in run(f"CONNECT localhost:443 HTTP/1.1\r\n{header}\r\n")[0]


@pytest.mark.parametrize("target", ["example.com:443", "localhost:80", "LOCALHOST.evil.com:443"])
def test_only_allowed_hosts_on_443(target):
    assert " 403 " in run(f"CONNECT {target} HTTP/1.1\r\n{auth()}\r\n")[0]


def test_plain_http_to_unlisted_target_refused():
    assert " 403 " in run(f"GET http://localhost/ HTTP/1.1\r\n{auth()}\r\n")[0]


# ---- plain HTTP forwarding to a local LLM service (e.g. CLIProxyAPI on 127.0.0.1:8317) ----

async def forward_roundtrip(password: str = TOKEN, allow_upstream: bool = True):
    """A real httpx client -> Proxy -> a tiny local HTTP server. Returns (response, what upstream saw)."""
    seen: dict = {}

    async def upstream_http(r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        lines = head.split("\r\n")
        headers = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        body = await r.readexactly(int(headers.get("content-length", 0)))
        seen.update(request_line=lines[0], headers=headers, body=body)
        reply = b'{"ok": true}'
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(reply), reply))
        await w.drain()
        w.close()

    upstream = await asyncio.start_server(upstream_http, "127.0.0.1", 0)
    target = f"127.0.0.1:{upstream.sockets[0].getsockname()[1]}"
    proxy = Proxy(TOKEN, set(), http_allowed={target} if allow_upstream else set())
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    proxy_url = f"http://kiri:{password}@127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with upstream, server:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=5) as client:
            resp = await client.post(f"http://{target}/v1/chat/completions", json={"q": "やっほー"},
                                     headers={"Authorization": "Bearer sk-llm"})
    return resp, seen


def test_forwards_plain_http_to_allowed_local_service():
    resp, seen = asyncio.run(forward_roundtrip())
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert seen["request_line"] == "POST /v1/chat/completions HTTP/1.1"  # origin-form upstream
    assert seen["headers"]["authorization"] == "Bearer sk-llm"  # the LLM key passes through
    assert "proxy-authorization" not in seen["headers"]  # the proxy password does not
    assert json.loads(seen["body"]) == {"q": "やっほー"}


def test_forward_needs_password_and_allowlist():
    assert asyncio.run(forward_roundtrip(password="wrong-password-xxxxx"))[0].status_code == 407
    assert asyncio.run(forward_roundtrip(allow_upstream=False))[0].status_code == 403
