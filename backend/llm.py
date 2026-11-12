"""LLM client for the analysis pipeline (OpenAI-compatible chat completions).

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
