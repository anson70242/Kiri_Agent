"""The LLM client's search-backed call (the gateway's Gemini-native endpoint), against a fake gateway."""

import asyncio
import json

from backend.llm import LLMSettings, ask_with_search
from backend.llm_proxy import Proxy

TOKEN = "s3cret-token-1234567"


def reply_with(*parts):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": "STOP"}],
            "usageMetadata": {"totalTokenCount": 10}}


async def ask(settings_for, model="song-model", prompt="哪首歌？", status=200, body=None):
    """Fake gateway + our Proxy; `settings_for(base_url, proxy_url)` builds the settings.
    Returns (reply, requests the gateway saw, proxy connection count)."""
    seen = []
    hits = {"proxy": 0}
    payload = json.dumps(reply_with({"text": "好的"}) if body is None else body).encode()

    async def gateway(r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        lines = head.split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        raw = await r.readexactly(int(headers.get("content-length", 0)))
        seen.append({"method": method, "path": path, "headers": headers, "body": json.loads(raw or b"null")})
        w.write(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                b"Connection: close\r\n\r\n%s" % (status, len(payload), payload))
        await w.drain()
        w.close()

    gateway_server = await asyncio.start_server(gateway, "127.0.0.1", 0)
    target = f"127.0.0.1:{gateway_server.sockets[0].getsockname()[1]}"
    proxy = Proxy(TOKEN, set(), http_allowed={target})

    async def counted(r, w):
        hits["proxy"] += 1
        await proxy.handle(r, w)

    proxy_server = await asyncio.start_server(counted, "127.0.0.1", 0)
    proxy_url = f"http://kiri:{TOKEN}@127.0.0.1:{proxy_server.sockets[0].getsockname()[1]}"
    async with gateway_server, proxy_server:
        settings = settings_for(f"http://{target}/v1", proxy_url)
        reply = await asyncio.to_thread(ask_with_search, settings, model, prompt)
    return reply, seen, hits["proxy"]


def direct(url, proxy):
    return LLMSettings(base_url=url, api_key="k-123", model="chat-model", proxy=None)


def test_asks_the_named_model_on_the_native_endpoint_with_search_declared():
    reply, seen, _ = asyncio.run(ask(direct, model="gemini-x-high", prompt="这是什么歌？"))
    assert reply == "好的"
    [req] = seen
    assert (req["method"], req["path"]) == ("POST", "/v1beta/models/gemini-x-high:generateContent")
    assert req["headers"]["x-goog-api-key"] == "k-123"
    assert req["body"] == {"contents": [{"role": "user", "parts": [{"text": "这是什么歌？"}]}],
                           "tools": [{"googleSearch": {}}]}


def test_reply_text_leaves_out_thought_parts():
    body = reply_with({"text": "", "thought": True}, {"text": "let me search", "thought": True},
                      {"text": '```json\n{"songs": []', "thoughtSignature": "abc"}, {"text": "}\n```"})
    reply, _, _ = asyncio.run(ask(direct, body=body))
    assert reply == '```json\n{"songs": []}\n```'


def test_proxy_on_goes_through_the_proxy():
    reply, seen, proxy_hits = asyncio.run(ask(
        lambda url, proxy: LLMSettings(base_url=url, api_key="k", model="m", proxy=proxy)))
    assert reply == "好的" and len(seen) == 1 and proxy_hits == 1


def test_proxy_off_connects_directly():
    reply, seen, proxy_hits = asyncio.run(ask(direct))
    assert reply == "好的" and len(seen) == 1 and proxy_hits == 0
