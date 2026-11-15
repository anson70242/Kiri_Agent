"""LLM client for the analysis pipeline. Two call paths to the same gateway:

- `chat`: OpenAI-compatible chat completion with the configured model (`llm.model`).
  Used for the segment, scoring and translation passes.
- `ask_with_search`: one prompt to a model named by the caller, on the gateway's
  Gemini-native endpoint (`<base_url without /v1>/v1beta/models/<model>:generateContent`)
  with the Google Search tool declared. It exists because the chat path cannot search:
  the configured chat model never searches (the tool is dropped without an error), and
  on the OpenAI endpoint no model could be confirmed to search. Song naming in a
  singing live uses it, with a search-capable model.

Both take the same settings: base URL, API key, proxy switch and timeout.

config.json:

    "llm": {
      "base_url": "http://127.0.0.1:8317/v1",   # e.g. CLIProxyAPI (Google agy) on the Japan PC
      "api_key_env": "AGY_API_KEY",             # .env variable holding the key
      "model": "gemini-3.8-flash-high",
      "proxy": true                             # send requests through LLM_PROXY
    }

`proxy: true` sends every request through `LLM_PROXY` (backend.llm_proxy on another
machine over Tailscale): the request then runs from that machine, so `127.0.0.1` in
`base_url` means *its* localhost. Only LLM requests take this path. `proxy: false`
(the default) connects directly; LLM_PROXY is ignored.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from openai import APIError, OpenAI


class LLMError(Exception):
    """Bad LLM settings, or the request failed."""


@dataclass
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    proxy: str | None = None  # LLM_PROXY URL when the switch is on
    timeout: float = 300


def load_llm(config: Path) -> LLMSettings:
    """Settings from config.json's `llm` section and the environment (.env)."""
    cfg = json.loads(config.read_text(encoding="utf-8")).get("llm") or {}
    missing = [k for k in ("base_url", "model") if not cfg.get(k)]
    if missing:
        raise LLMError(f"config.json llm section is missing {', '.join(missing)}")
    key_env = cfg.get("api_key_env") or "LLM_API_KEY"
    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        raise LLMError(f"set {key_env} in .env")
    proxy = None
    if cfg.get("proxy"):
        proxy = os.getenv("LLM_PROXY", "").strip()
        if not proxy:
            raise LLMError('llm.proxy is on but LLM_PROXY is not set in .env (or set "proxy": false)')
    return LLMSettings(cfg["base_url"], api_key, cfg["model"], proxy, float(cfg.get("timeout", 300)))


def make_client(settings: LLMSettings) -> OpenAI:
    http = httpx.Client(proxy=settings.proxy, timeout=settings.timeout) if settings.proxy else None
    return OpenAI(base_url=settings.base_url, api_key=settings.api_key, http_client=http,
                  timeout=settings.timeout, max_retries=2)


def chat(settings: LLMSettings, messages: list[dict[str, Any]], client: OpenAI | None = None, **kw: Any) -> str:
    """One chat completion; returns the reply text."""
    own = client is None
    client = client or make_client(settings)
    try:
        resp = client.chat.completions.create(model=settings.model, messages=messages, **kw)
    except APIError as e:
        raise LLMError(f"LLM request failed: {e}") from e
    finally:
        if own:
            client.close()
    return resp.choices[0].message.content or ""


class _TemporaryFailure(LLMError):
    """No connection, a timeout, HTTP 429 or 5xx: worth another try."""


def ask_with_search(settings: LLMSettings, model: str, prompt: str,
                    retry_waits: Sequence[float] = (2.0, 5.0)) -> str:
    """One prompt to `model` with Google Search available; returns the reply text. A failure
    that looks temporary is retried after each of `retry_waits` seconds (twice), as the chat
    call's client does; a reply without text raises."""
    root = settings.base_url.rstrip("/").removesuffix("/v1")
    url = f"{root}/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "tools": [{"googleSearch": {}}]}
    for wait in retry_waits:
        try:
            return _search_once(settings, url, body)
        except _TemporaryFailure:
            time.sleep(wait)
    return _search_once(settings, url, body)


def _search_once(settings: LLMSettings, url: str, body: dict[str, Any]) -> str:
    try:
        with httpx.Client(proxy=settings.proxy, timeout=settings.timeout) as http:
            resp = http.post(url, json=body, headers={"x-goog-api-key": settings.api_key})
    except httpx.TransportError as e:
        raise _TemporaryFailure(f"LLM request failed: {type(e).__name__}: {e}") from e
    except httpx.HTTPError as e:
        raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e
    if resp.is_error:
        failure = _TemporaryFailure if resp.status_code == 429 or resp.status_code >= 500 else LLMError
        raise failure(f"LLM request failed: HTTP {resp.status_code}: {_error_message(resp)}")
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
        texts = [p.get("text", "") for p in parts if not p.get("thought")]
        if not all(isinstance(t, str) for t in texts):
            raise TypeError("a part's text is not a string")
    except (ValueError, LookupError, TypeError, AttributeError) as e:
        raise LLMError(f"LLM returned an unexpected reply ({type(e).__name__}: {e}): {resp.text[:200]}") from e
    if not (text := "".join(texts)):
        raise LLMError(f"LLM reply has no text (only thoughts, or no parts): {resp.text[:200]}")
    return text


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json()["error"]["message"])
    except (ValueError, LookupError, TypeError):
        return resp.text[:200]
