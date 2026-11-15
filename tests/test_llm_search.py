"""The LLM client's search-backed call (the gateway's Gemini-native endpoint), against a fake gateway."""

import asyncio
import json
import socket
import time

import pytest

from backend.llm import LLMError, LLMSettings, ask_with_search
from backend.llm_proxy import Proxy

TOKEN = "s3cret-token-1234567"


def reply_with(*parts):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": "STOP"}],
            "usageMetadata": {"totalTokenCount": 10}}


OK = reply_with({"text": "好的"})


async def ask(settings_for, model="song-model", prompt="哪首歌？", status=200, body=None, hang=False, script=None,
              seen=None):
    """Fake gateway + our Proxy; `settings_for(base_url, proxy_url)` builds the settings.
    `body` is the gateway's reply (a str is sent as is); `hang` never replies, until the client hangs up.
    `script` answers request n with its n-th item (the last repeats): a (status, body) pair, or
    "drop" to close the connection without a reply. Retries do not wait here.
    Returns (reply, requests the gateway saw, proxy connection count); the requests also go
    into `seen` when given, so a test can count them when the call raises."""
    seen = [] if seen is None else seen
    hits = {"proxy": 0}
    script = script or [(status, OK if body is None else body)]

    async def gateway(r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        lines = head.split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        raw = await r.readexactly(int(headers.get("content-length", 0)))
        seen.append({"method": method, "path": path, "headers": headers, "body": json.loads(raw or b"null")})
        if hang:
            await r.read()
            w.close()
            return
        answer = script[min(len(seen), len(script)) - 1]
        if answer == "drop":
            w.close()
            return
        status, body = answer
        payload = (body if isinstance(body, str) else json.dumps(body)).encode()
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
        reply = await asyncio.to_thread(ask_with_search, settings, model, prompt, retry_waits=(0, 0))
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


def test_error_status_raises_with_the_status_and_message():
    body = {"error": {"code": 429, "message": "quota exhausted", "status": "RESOURCE_EXHAUSTED"}}
    with pytest.raises(LLMError, match=r"429.*quota exhausted"):
        asyncio.run(ask(direct, status=429, body=body))


@pytest.mark.parametrize("body", [
    "not json at all",
    {"candidates": []},
    {"promptFeedback": {"blockReason": "SAFETY"}},
    {"candidates": [{"finishReason": "SAFETY"}]},
    {"candidates": [{"content": {"role": "model"}}]},
    {"candidates": [{"content": {"parts": ["junk"]}}]},
    {"candidates": [{"content": {"parts": [{"text": 5}]}}]},
    ["candidates"],
], ids=["not-json", "no-candidates", "blocked", "no-content", "no-parts", "part-not-object", "text-not-string", "list"])
def test_reply_without_the_expected_shape_raises(body):
    with pytest.raises(LLMError, match="unexpected reply"):
        asyncio.run(ask(direct, body=body))


@pytest.mark.parametrize("parts", [[], [{"text": "searching", "thought": True}], [{"thoughtSignature": "abc"}],
                                   [{"text": ""}]], ids=["no-parts", "only-thoughts", "no-text", "empty-text"])
def test_reply_without_text_raises(parts):
    seen = []
    with pytest.raises(LLMError, match="no text"):
        asyncio.run(ask(direct, body=reply_with(*parts), seen=seen))
    assert len(seen) == 1  # not a temporary failure


@pytest.mark.parametrize("failure", [(503, {"error": {"message": "overloaded"}}), (500, "oops"),
                                     (429, {"error": {"message": "slow down"}}), "drop"])
def test_a_temporary_failure_is_retried(failure):
    reply, seen, _ = asyncio.run(ask(direct, script=[failure, (200, OK)]))
    assert reply == "好的" and len(seen) == 2


def test_a_temporary_failure_is_retried_twice_then_raises():
    seen = []
    with pytest.raises(LLMError, match=r"500.*busy"):
        asyncio.run(ask(direct, script=[(503, "overloaded"), (502, "down"), (500, "busy"), (200, OK)], seen=seen))
    assert len(seen) == 3  # the first try and two retries


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_client_errors_are_not_retried(status):
    seen = []
    with pytest.raises(LLMError, match=str(status)):
        asyncio.run(ask(direct, script=[(status, {"error": {"message": "bad"}}), (200, OK)], seen=seen))
    assert len(seen) == 1


def test_unreachable_gateway_raises():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = LLMSettings(base_url=f"http://127.0.0.1:{port}/v1", api_key="k", model="m", timeout=5)
    with pytest.raises(LLMError, match="request failed"):
        # no retries: a refused connection takes ~2 s on Windows (retrying is covered by "drop" above)
        ask_with_search(settings, "song-model", "哪首歌？", retry_waits=())


def test_settings_timeout_applies():
    started = time.monotonic()
    seen = []
    with pytest.raises(LLMError, match="request failed"):
        asyncio.run(ask(lambda url, proxy: LLMSettings(base_url=url, api_key="k", model="m", timeout=0.3),
                        hang=True, seen=seen))
    assert len(seen) == 3  # a timeout is retried twice
    assert time.monotonic() - started < 5  # 3 attempts of 0.3 s each, plus start-up


def test_proxy_on_goes_through_the_proxy():
    reply, seen, proxy_hits = asyncio.run(ask(
        lambda url, proxy: LLMSettings(base_url=url, api_key="k", model="m", proxy=proxy)))
    assert reply == "好的" and len(seen) == 1 and proxy_hits == 1


def test_proxy_off_connects_directly():
    reply, seen, proxy_hits = asyncio.run(ask(direct))
    assert reply == "好的" and len(seen) == 1 and proxy_hits == 0
