"""A fake flash service for the flash service client's tests: a real WebSocket that answers
actions by their `echo` (and sends unrelated event frames before every answer), and the
login interface over real HTTP, both on 127.0.0.1. The answers have the shapes the flash
service gave on 2026-11-23; ids, codes and addresses are made up.

    with FakeFlashService() as service:
        client = FlashClient(service.settings(), downloads)
        service.online = False                    # get_status answers offline
        service.answers["some_action"] = lambda params: {"data": {...}}   # script an action
        service.actions                           # every action frame received, in order
        service.login_calls                       # (path, Authorization header, body) per HTTP call

A flash transfer (`create_flash_task`, `get_fileset_info`) answers by default:

        service.created                           # the params of every create_flash_task, in order
        service.progress = [fileset(...), ...]    # one per get_fileset_info, the last repeated

The real flash service's states and limits (seen on the server on 2026-11-23 and 24):

- Account online: modelled (`online`, `logged_in`).
- Account offline, the login interface holds a QR code: modelled (`online = False`).
- Started and not logged in yet: it answers no action, only the login interface. Not
  modelled by the fake itself: a test points the actions' address at `closed_port()`.
- Account logged out by QQ: the login interface keeps the old QR code, which has run out, and
  refuses a new one until the flash service is restarted. Not modelled (the fake refuses a
  new QR code only while `logged_in`); nothing in this service can act on that state.
- The login interface takes a few logins a minute from one caller and refuses the rest.
  Not modelled: the tests count the logins instead (`login_calls`).
- A credential runs out after an hour: modelled by changing `credential`. The real refusal
  was not seen (the service's own source has an answer with an error code); the fake's is
  HTTP 401. The client treats both alike.
- A flash transfer that fails, a file that fails: the answers were never seen. The fake's
  are guesses (`failed` counts in `fileset`).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from websockets.sync.server import ServerConnection, serve

from backend.flash.client import FlashSettings

LOGIN_HASH = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"  # made up
CREDENTIAL = "made-up-credential-0123456789"
QR_CODE = "https://qr.example/login?k=made-up-qr-0001"
NEW_QR_CODE = "https://qr.example/login?k=made-up-qr-0002"
GROUP_TEXT = "群里的一句话：今晚八点开播"  # inside an event frame: never read, never logged
FILESET_ID = "made-up-fileset-7f3a"
FLASH_LINK = "https://flash.example/q/MadeUpLink7f3a"
EXPIRE_TIME = 1_796_400_000  # 2026-12-04 16:00 UTC, in seconds (the unit is a guess: not recorded)


def fileset(sent: int, total: int, *, done: int = 0, failed: int = 0, finished: bool = False) -> dict[str, Any]:
    """get_fileset_info's `data.fileSet`: uploadStatus 2 while uploading, 4 when done."""
    return {"fileSetId": FILESET_ID, "totalFileSize": total, "uploadStatus": 4 if finished else 2,
            "uploadInfo": {"totalUploadedFileSize": sent, "successCount": done, "failedCount": failed}}

# Frames the flash service sends on its own: the client drops them unread.
EVENTS: list[str] = [
    json.dumps({"kind": "event", "group": 123456789, "text": GROUP_TEXT}, ensure_ascii=False),
    json.dumps({"echo": "somebody-else", "data": {"online": False}}),  # an answer to another caller
    "not json at all",
    "[1, 2, 3]",
]

Answer = Callable[[dict[str, Any]], dict[str, Any]]

# The fake's own server logs every frame at debug level; the tests read the log for what the
# client writes, so the fake writes nothing.
_quiet = logging.getLogger("tests.flash_fakes.server")
_quiet.propagate = False
_quiet.setLevel(logging.CRITICAL + 1)


def closed_port() -> int:
    """A port nothing listens on: a flash service that cannot be reached."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeFlashService:
    def __init__(self) -> None:
        self.online = True  # get_status's data.online
        self.logged_in = False  # the login interface's data.isLogin
        self.qr_code = QR_CODE  # data.qrcodeurl while not logged in
        self.login_error = ""  # data.loginError
        self.login_hash = LOGIN_HASH
        self.credential = CREDENTIAL  # what a login answers and the other calls take; change it to run one out
        self.events = list(EVENTS)
        self.answers: dict[str, Answer] = {"get_status": lambda params: {"data": {"online": self.online, "good": True}},
                                           "create_flash_task": self._create, "get_fileset_info": self._fileset_info}
        self.created: list[dict[str, Any]] = []
        self.progress: list[dict[str, Any]] = [fileset(100, 100, done=1, finished=True)]
        self.actions: list[dict[str, Any]] = []
        self.login_calls: list[tuple[str, str | None, Any]] = []
        self.qr_refreshes = 0

    # ---- lifetime ----

    def __enter__(self) -> FakeFlashService:
        self._ws = serve(self._serve_actions, "127.0.0.1", 0, logger=_quiet)
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), self._login_handler())
        self._threads = [threading.Thread(target=self._ws.serve_forever, daemon=True),
                         threading.Thread(target=self._http.serve_forever, daemon=True)]
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._ws.shutdown()
        self._http.shutdown()
        self._http.server_close()
        for t in self._threads:
            t.join(5)

    @property
    def actions_url(self) -> str:
        return f"ws://127.0.0.1:{self._ws.socket.getsockname()[1]}/?access_token=made-up-token"

    @property
    def login_url(self) -> str:
        return f"http://127.0.0.1:{self._http.server_address[1]}"

    def env(self, **overrides: str | None) -> dict[str, str]:
        """The four variables as `.env` would hold them; an override of None leaves one out."""
        env = {"FLASH_ACTIONS_URL": self.actions_url, "FLASH_LOGIN_URL": self.login_url,
               "FLASH_LOGIN_HASH": self.login_hash, "FLASH_DOWNLOADS_PATH": "/data/downloads"}
        env.update(overrides)  # type: ignore[arg-type]
        return {k: v for k, v in env.items() if v is not None}

    def settings(self, **overrides: str | None) -> FlashSettings:
        settings = FlashSettings.from_env(self.env(**overrides))
        assert settings is not None
        return settings

    # ---- the actions (WebSocket) ----

    def _serve_actions(self, connection: ServerConnection) -> None:
        for message in connection:
            frame = json.loads(message)
            self.actions.append(frame)
            answer = self.answers.get(frame.get("action"))
            body = answer(frame.get("params") or {}) if answer else {"data": None, "message": "unknown action"}
            for event in self.events:
                connection.send(event)
            connection.send(json.dumps({**body, "echo": frame.get("echo")}, ensure_ascii=False))

    def _create(self, params: dict[str, Any]) -> dict[str, Any]:
        self.created.append(params)
        return {"status": "ok", "retcode": 0, "message": "",
                "data": {"createFlashTransferResult": {"fileSetId": FILESET_ID, "shareLink": FLASH_LINK,
                                                       "expireTime": EXPIRE_TIME}}}

    def _fileset_info(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("fileset_id") != FILESET_ID:
            return {"status": "failed", "retcode": 1200, "data": None, "message": "文件集不存在"}
        step = self.progress.pop(0) if len(self.progress) > 1 else self.progress[0]
        return {"status": "ok", "retcode": 0, "message": "", "data": {"fileSet": step}}

    # ---- the login interface (HTTP) ----

    def _login_handler(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # quiet
                pass

            def do_POST(self) -> None:  # noqa: N802 (the http.server name)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                auth = self.headers.get("Authorization")
                service.login_calls.append((self.path, auth, body))
                status, answer = service._login_answer(self.path, auth, body)
                data = json.dumps(answer, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def _login_answer(self, path: str, auth: str | None, body: Any) -> tuple[int, dict[str, Any]]:
        if path == "/api/auth/login":
            if not isinstance(body, dict) or body.get("hash") != self.login_hash:
                return 200, {"code": -1, "message": "密钥不对", "data": None}
            return 200, {"code": 0, "message": "success", "data": {"Credential": self.credential}}
        if auth != f"Bearer {self.credential}":
            return 401, {"code": -1, "message": "Unauthorized", "data": None}
        if path == "/api/QQLogin/CheckLoginStatus":
            return 200, {"code": 0, "message": "success",
                         "data": {"isLogin": self.logged_in, "isOffline": not self.logged_in,
                                  "qrcodeurl": "" if self.logged_in else self.qr_code,
                                  "loginError": self.login_error}}
        if path == "/api/QQLogin/RefreshQRcode":
            if self.logged_in:
                return 200, {"code": -1, "message": "已经登录了，不用二维码", "data": None}
            self.qr_refreshes += 1
            self.qr_code = NEW_QR_CODE
            return 200, {"code": 0, "message": "success", "data": None}
        return 404, {"code": -1, "message": "not found", "data": None}
