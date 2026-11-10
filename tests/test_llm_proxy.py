"""LLM proxy: password, host allowlist, and an actual tunnel to a local echo server."""

import asyncio
import base64

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


def test_plain_http_refused():
    assert " 405 " in run(f"GET http://localhost/ HTTP/1.1\r\n{auth()}\r\n")[0]
