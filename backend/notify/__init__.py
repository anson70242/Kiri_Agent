"""Go-live / stream-ended notifications. Importing a channel module registers it."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from . import discord  # noqa: F401  (registers each Notifier)
from .base import EventKind, LiveNotifier, Notifier, PermanentNotifyError, StreamEvent

__all__ = ["EventKind", "LiveNotifier", "Notifier", "PermanentNotifyError", "StreamEvent", "load_notifier"]

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_notifier(config: Path) -> LiveNotifier | None:
    """Build a LiveNotifier from config.json's `notify` section and .env secrets.

    "notify": {
      "on_startup": false,
      "on_offline": true,
      "offline_grace": 120,
      "discord": { "webhook_env": "DISCORD_WEBHOOK_URL", "mention": "@everyone" }
    }

    A channel whose webhook env var is empty is skipped with a warning.
    Returns None when no channel is usable.
    """
    load_dotenv(REPO_ROOT / ".env")
    cfg = json.loads(config.read_text(encoding="utf-8")).get("notify") or {}

    notifiers: list[Notifier] = []
    for name, opts in cfg.items():
        if name not in Notifier.registry or not isinstance(opts, dict):
            continue
        if opts.get("enabled") is False:
            continue
        env = opts.get("webhook_env", f"{name.upper()}_WEBHOOK_URL")
        url = os.getenv(env, "").strip()
        if not url:
            log.warning("notify.%s: %s is not set in .env, skipping", name, env)
            continue
        notifiers.append(Notifier.registry[name](url, mention=opts.get("mention")))

    if not notifiers:
        return None
    state_file = Path(cfg.get("state_file", "data/notify_state.json"))
    return LiveNotifier(
        notifiers,
        notify_on_startup=bool(cfg.get("on_startup", False)),
        notify_offline=bool(cfg.get("on_offline", True)),
        offline_grace=float(cfg.get("offline_grace", 120)),
        state_file=state_file if state_file.is_absolute() else REPO_ROOT / state_file,
    )
