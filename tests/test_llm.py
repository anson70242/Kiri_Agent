"""LLM client settings: the proxy on/off switch (config `llm.proxy`)."""

import asyncio
import json

import pytest

from backend.llm import LLMError, LLMSettings, chat, load_llm
from backend.llm_proxy import Proxy

TOKEN = "s3cret-token-1234567"
COMPLETION = {
    "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "好的"}}],
}


async def call_llm(settings_for):
    """Fake LLM server + our Proxy; `settings_for(llm_url, proxy_url)` builds the settings.
    Returns (reply, upstream request count, proxy request count)."""
    hits = {"llm": 0, "proxy": 0}

    async def llm(r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        length = next((int(ln.split(":")[1]) for ln in head.split("\r\n") if ln.lower().startswith("content-length")), 0)
        await r.readexactly(length)
        hits["llm"] += 1
        body = json.dumps(COMPLETION).encode()
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                b"Connection: close\r\n\r\n%s" % (len(body), body))
        await w.drain()
        w.close()

    llm_server = await asyncio.start_server(llm, "127.0.0.1", 0)
    target = f"127.0.0.1:{llm_server.sockets[0].getsockname()[1]}"
    proxy = Proxy(TOKEN, set(), http_allowed={target})

    async def counted(r, w):
        hits["proxy"] += 1
        await proxy.handle(r, w)

    proxy_server = await asyncio.start_server(counted, "127.0.0.1", 0)
    proxy_url = f"http://kiri:{TOKEN}@127.0.0.1:{proxy_server.sockets[0].getsockname()[1]}"
    async with llm_server, proxy_server:
        settings = settings_for(f"http://{target}/v1", proxy_url)
        reply = await asyncio.to_thread(chat, settings, [{"role": "user", "content": "hi"}])
    return reply, hits["llm"], hits["proxy"]


def test_proxy_on_goes_through_the_proxy():
    reply, llm_hits, proxy_hits = asyncio.run(call_llm(
        lambda url, proxy: LLMSettings(base_url=url, api_key="k", model="m", proxy=proxy)))
    assert reply == "好的" and llm_hits == 1 and proxy_hits == 1


def test_proxy_off_connects_directly():
    reply, llm_hits, proxy_hits = asyncio.run(call_llm(
        lambda url, proxy: LLMSettings(base_url=url, api_key="k", model="m", proxy=None)))
    assert reply == "好的" and llm_hits == 1 and proxy_hits == 0


def write_config(tmp_path, llm):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"llm": llm}), encoding="utf-8")
    return cfg


def test_load_llm_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_PROXY", "http://kiri:x@100.1.2.3:8118")
    base = {"base_url": "http://127.0.0.1:8317/v1", "api_key_env": "AGY_API_KEY", "model": "gemini-3.8-flash-high"}

    on = load_llm(write_config(tmp_path, base | {"proxy": True}))
    assert (on.base_url, on.api_key, on.model, on.proxy) == (
        "http://127.0.0.1:8317/v1", "sk-test", "gemini-3.8-flash-high", "http://kiri:x@100.1.2.3:8118")

    off = load_llm(write_config(tmp_path, base | {"proxy": False}))
    assert off.proxy is None  # LLM_PROXY is ignored when the switch is off


def test_proxy_on_without_llm_proxy_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_API_KEY", "sk-test")
    monkeypatch.delenv("LLM_PROXY", raising=False)
    cfg = write_config(tmp_path, {"base_url": "http://x/v1", "api_key_env": "AGY_API_KEY", "model": "m", "proxy": True})
    with pytest.raises(LLMError, match="LLM_PROXY"):
        load_llm(cfg)
