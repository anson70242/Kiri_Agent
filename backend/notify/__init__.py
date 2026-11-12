"""Go-live / stream-ended notifications. Importing a channel module registers it."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from . import discord  # noqa: F401  (registers each Notifier)
from .base import EventKind, LiveNotifier, Notifier, PermanentNotifyError, StreamEvent
from .history import NotifyHistory
from .qq import QQGroups, QQNotifier, QQSettings

__all__ = ["EventKind", "LiveNotifier", "Notifier", "PermanentNotifyError", "StreamEvent", "load_notifier"]

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_notifier(config: Path, allow_empty: bool = False) -> LiveNotifier | None:
    """Build a LiveNotifier from config.json's `notify` section and .env secrets.

    "notify": {
      "on_startup": false,
      "on_offline": true,
      "offline_grace": 120,
      "discord": { "webhook_env": "DISCORD_WEBHOOK_URL", "mention": "@everyone" },
      "qq": { "app_id_env": "QQ_BOT_APP_ID", "secret_env": "QQ_BOT_SECRET" }
    }

    A channel whose webhook env var is empty is skipped with a warning; QQ joins only
    when both its bot credentials are set (groups: data/qq_groups.json). Every delivery
    is kept in data/notify_log.json for the 通知 page.
    Returns None when no channel is usable, unless `allow_empty` (the stream-ended
    tracking is still needed, e.g. for auto-download).
    """
    load_dotenv(REPO_ROOT / ".env")
    cfg = json.loads(config.read_text(encoding="utf-8")).get("notify") or {}

    notifiers: list[Notifier] = []
    for name, opts in cfg.items():
        if name not in Notifier.registry or name == "qq" or not isinstance(opts, dict):
            continue
        if opts.get("enabled") is False:
            continue
        env = opts.get("webhook_env", f"{name.upper()}_WEBHOOK_URL")
        url = os.getenv(env, "").strip()
        if not url:
            log.warning("notify.%s: %s is not set in .env, skipping", name, env)
            continue
        notifiers.append(Notifier.registry[name](url, mention=opts.get("mention")))

    if qq := load_qq(cfg.get("qq")):
        notifiers.append(qq)

    if not notifiers and not allow_empty:
        return None
    state_file = Path(cfg.get("state_file", "data/notify_state.json"))
    log_file = Path(cfg.get("log_file", "data/notify_log.json"))
    history = NotifyHistory(log_file if log_file.is_absolute() else REPO_ROOT / log_file)
    if qq:
        qq.history = history  # one list of what was sent, every channel
    return LiveNotifier(
        notifiers,
        notify_on_startup=bool(cfg.get("on_startup", False)),
        notify_offline=bool(cfg.get("on_offline", True)),
        offline_grace=float(cfg.get("offline_grace", 120)),
        state_file=state_file if state_file.is_absolute() else REPO_ROOT / state_file,
        history=history,
    )


def load_qq(opts: object) -> QQNotifier | None:
    """The QQ bot channel, or None when it's off or its credentials aren't in .env."""
    if not isinstance(opts, dict) or opts.get("enabled") is False:
        return None
    app_id = os.getenv(opts.get("app_id_env") or "QQ_BOT_APP_ID", "").strip()
    secret = os.getenv(opts.get("secret_env") or "QQ_BOT_SECRET", "").strip()
    if not (app_id and secret):
        log.info("notify.qq: QQ bot credentials not in .env, QQ off")
        return None
    groups = Path(opts.get("groups_file") or "data/qq_groups.json")
    return QQNotifier(QQSettings(app_id, secret), QQGroups(groups if groups.is_absolute() else REPO_ROOT / groups))
