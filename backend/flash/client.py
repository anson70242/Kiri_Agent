"""The flash service client: the only place that knows how the flash service is spoken to
(docs/adr/0003-flash-transfers-through-an-outside-qq-client-service.md).

The settings come from the environment (`.env`), never from config.json:

    FLASH_ACTIONS_URL     the address of the actions (WebSocket); without it the feature is off
    FLASH_LOGIN_URL       the address of the login interface (HTTP)
    FLASH_LOGIN_HASH      what the login interface expects in place of its token
    FLASH_DOWNLOADS_PATH  how the flash service sees this service's downloads folder

An action is one JSON frame `{"action", "params", "echo"}` on a connection of its own; its
answer is the frame with the same `echo`. Every other frame is an event of the flash account
(a group's message, ...): it is dropped unread.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from dotenv import load_dotenv
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.sync.client import connect

REPO_ROOT = Path(__file__).resolve().parents[2]

ACTIONS_URL_ENV = "FLASH_ACTIONS_URL"
LOGIN_URL_ENV = "FLASH_LOGIN_URL"
LOGIN_HASH_ENV = "FLASH_LOGIN_HASH"
DOWNLOADS_PATH_ENV = "FLASH_DOWNLOADS_PATH"
DEFAULT_DOWNLOADS_PATH = "/data/downloads"

ACTION_TIMEOUT = 30.0  # seconds for an action's answer, connecting included
LOGIN_TIMEOUT = 15.0  # seconds for one call of the login interface

# The login interface is called with the standard library, which writes no request log (the
# usual HTTP client's debug lines name the address), and past any proxy of the environment:
# the flash service runs beside this service.
_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Handed to the WebSocket library in place of its own logger: its debug lines print the
# frames (an event holds a group's message, an answer a flash link) and the address.
_wire_log = logging.getLogger(f"{__name__}.wire")
_wire_log.propagate = False
_wire_log.setLevel(logging.CRITICAL + 1)
_wire_log.addHandler(logging.NullHandler())


class FlashUnavailable(Exception):
    """The flash account is offline or the flash service cannot be reached: wait, do not fail."""


class FlashFailed(Exception):
    """Any other failure; the message says why (the flash service's own words when it gave any)."""


@dataclass(frozen=True)
class FlashSettings:
    actions_url: str
    login_url: str | None
    login_hash: str | None
    downloads_path: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> FlashSettings | None:
        """The settings `env` holds, or None without the address of the actions."""
        actions = (env.get(ACTIONS_URL_ENV) or "").strip()
        if not actions:
            return None
        return cls(actions_url=actions,
                   login_url=(env.get(LOGIN_URL_ENV) or "").strip() or None,
                   login_hash=(env.get(LOGIN_HASH_ENV) or "").strip() or None,
                   downloads_path=(env.get(DOWNLOADS_PATH_ENV) or "").strip() or DEFAULT_DOWNLOADS_PATH)


def flash_configured(env: Mapping[str, str] | None = None) -> bool:
    """Whether a flash service is configured (`env`: the process environment by default,
    which holds `.env` once the service has loaded its settings)."""
    return FlashSettings.from_env(os.environ if env is None else env) is not None


def load_flash(config: Path, env: Mapping[str, str] | None = None) -> FlashClient | None:
    """The client of the configured flash service, or None when none is configured.
    `env`: the process environment with `.env` loaded, by default. The downloads folder is
    config.json's."""
    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    settings = FlashSettings.from_env(env)
    if settings is None:
        return None
    # Here, not at the top: the pipeline and the dashboard, which import this module, sit
    # under the modules this one would import.
    from backend.downloader import load_settings

    return FlashClient(settings, load_settings(config)[0].output_dir)


@dataclass(frozen=True)
class AccountState:
    """The flash account's state. `qr_code` (the text to draw) and `error` (the flash
    service's own words) only while offline; `missing` says which setting keeps the QR code
    from being read ("" when none)."""

    state: Literal["online", "offline", "unreachable"]
    qr_code: str | None = None
    error: str = ""
    missing: str = ""


class FlashClient:
    """The flash service, for this service's downloads folder `downloads`. Every call blocks
    (one connection per call, at most ACTION_TIMEOUT or LOGIN_TIMEOUT per round trip): the
    pipeline calls it from its stage threads, and async code through `asyncio.to_thread`, as
    the drive is called. Holds no connection between calls."""

    def __init__(self, settings: FlashSettings, downloads: Path):
        self.settings = settings
        self.downloads = downloads

    def account(self) -> AccountState:
        try:
            status = self._action("get_status", {})
        except FlashUnavailable:
            return AccountState("unreachable")
        if status.get("online") is True:
            return AccountState("online")
        try:
            login = self._login_status()
        except (FlashUnavailable, FlashFailed) as e:
            return AccountState("offline", error=str(e))
        if login is None:
            return AccountState("offline", missing=self._login_missing())
        qr_code = login.get("qrcodeurl")
        error = login.get("loginError")
        return AccountState("offline", qr_code=qr_code if isinstance(qr_code, str) and qr_code else None,
                            error=error.strip() if isinstance(error, str) else "")

    def flash_path(self, path: Path) -> str:
        """`path`, under this service's downloads folder, as the flash service sees it.
        FlashFailed for a path that is not under the downloads folder."""
        try:
            relative = path.resolve().relative_to(self.downloads.resolve())
        except ValueError:
            raise FlashFailed("文件不在下载目录里，闪传服务看不到") from None
        if not relative.parts:
            raise FlashFailed("下载目录本身不是一个文件")
        return str(PurePosixPath(self.settings.downloads_path, relative.as_posix()))

    def require_online(self) -> None:
        """Returns while the flash account is online. FlashUnavailable while it is offline or
        the flash service cannot be reached (wait); FlashFailed for anything else."""
        if self._action("get_status", {}).get("online") is not True:
            raise FlashUnavailable("闪传账号掉线")

    def refresh_qr_code(self) -> None:
        """Asks the flash service for a new QR code (the next `account()` gives it).
        FlashUnavailable when the login interface cannot be reached; FlashFailed when it is
        not set up or refuses (as it does while the account is logged in)."""
        if missing := self._login_missing():
            raise FlashFailed(missing)
        self._login_call("/api/QQLogin/RefreshQRcode")

    # ---- the login interface ----

    def _login_missing(self) -> str:
        """Which setting keeps the login interface from being used ("" when none)."""
        if not self.settings.login_url:
            return f"没有设置登录接口的地址（.env 里的 {LOGIN_URL_ENV}），看不到二维码"
        if not self.settings.login_hash:
            return f"没有设置登录哈希（.env 里的 {LOGIN_HASH_ENV}），看不到二维码"
        return ""

    def _login_status(self) -> dict[str, Any] | None:
        """CheckLoginStatus's data, or None while the login interface is not set up."""
        if self._login_missing():
            return None
        return self._login_call("/api/QQLogin/CheckLoginStatus") or {}

    def _login_call(self, path: str) -> Any:
        """Logs in with the login hash, then posts to `path` with the credential as bearer;
        answers the answer's data."""
        login = self._post("/api/auth/login", {"hash": self.settings.login_hash}, None)
        credential = login.get("Credential") if isinstance(login, dict) else None
        if not isinstance(credential, str) or not credential:
            raise FlashFailed("登录接口没有给出凭据")
        return self._post(path, {}, credential)

    def _post(self, path: str, body: dict[str, Any], credential: str | None) -> Any:
        """The answer's data. FlashUnavailable when the login interface cannot be reached,
        FlashFailed when it answers an error. No exception's own text is kept: it can hold
        the address."""
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        url = f"{self.settings.login_url.rstrip('/')}{path}"  # type: ignore[union-attr]
        request = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
        try:
            with _http.open(request, timeout=LOGIN_TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            answer = _json(raw)
            raise FlashFailed(f"登录接口答 HTTP {e.code}：{_reason(answer) if answer else ''}".rstrip("：")) from None
        except (OSError, TimeoutError) as e:  # URLError is an OSError
            raise FlashUnavailable(f"连不上闪传服务的登录接口（{type(e).__name__}）") from None
        answer = _json(raw)
        if answer is None:
            raise FlashFailed("登录接口的回答不是 JSON")
        if answer.get("code") not in (None, 0):
            raise FlashFailed(f"登录接口：{_reason(answer)}")
        return answer.get("data")

    # ---- the actions ----

    def _action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """The answer's `data`. Raises FlashUnavailable when the flash service cannot be
        reached or does not answer in time, FlashFailed when its answer holds no data."""
        echo = uuid.uuid4().hex
        deadline = time.monotonic() + ACTION_TIMEOUT
        try:
            with connect(self.settings.actions_url, open_timeout=ACTION_TIMEOUT, proxy=None,
                         max_size=2**24, logger=_wire_log) as ws:
                ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
                while True:
                    answer = _answer(ws.recv(timeout=max(deadline - time.monotonic(), 0.001)), echo)
                    if answer is not None:
                        break
        except InvalidURI:
            raise FlashFailed(f"{ACTIONS_URL_ENV} 不是 WebSocket 地址") from None
        # The exceptions' own text can name the address: only their kind is kept.
        except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake) as e:
            raise FlashUnavailable(f"连不上闪传服务（{type(e).__name__}）") from None
        data = answer.get("data")
        if not isinstance(data, dict):
            raise FlashFailed(f"{action}：{_reason(answer)}")
        return data


def _json(raw: str | bytes) -> dict[str, Any] | None:
    """A JSON object, or None for anything else."""
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _answer(frame: str | bytes, echo: str) -> dict[str, Any] | None:
    """The frame as the answer with this echo, or None for anything else (an event)."""
    value = _json(frame)
    return value if value is not None and value.get("echo") == echo else None


def _reason(answer: dict[str, Any]) -> str:
    message = answer.get("message")
    return message.strip() if isinstance(message, str) and message.strip() else "闪传服务的回答里没有数据"
