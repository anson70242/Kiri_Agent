"""Dashboard accounts: users, password hashing, login sessions, login rate limit, and the CLI.

    uv run -m backend.auth add-user <name> --admin    # asks for the password twice
    uv run -m backend.auth add-user <name>            # a member
    echo "$PW" | uv run -m backend.auth add-user <name> --password-stdin
    uv run -m backend.auth list
    uv run -m backend.auth passwd <name>
    uv run -m backend.auth disable <name>             # (enable <name> undoes it)
    # docker: docker compose exec kiri /app/.venv/bin/python -m backend.auth ...

config.json:
    "auth": {
        "enabled": true,                      # off (or no "auth" section): no login at all
        "users_file": "data/users.json",      # relative to the config's folder
        "sessions_file": "data/sessions.json",
        "trusted_proxies": ["172.29.70.10"]
    }

`trusted_proxies`: addresses (or networks) of the reverse proxy. Only when the direct peer
is one of them are X-Forwarded-For (first address: the client IP for the login rate
limit), X-Forwarded-Proto and X-Forwarded-Host believed; otherwise the socket peer and the
request's own scheme/host count. The default is Caddy's fixed address on the docker
network `edge`. Requests through Tailscale arrive from the docker gateway and are
deliberately not trusted (tailscale serve may add its own X-Forwarded-For).

The server and the CLI share the JSON files; the server re-reads a file when it changes
on disk, so a user added (or disabled) with the CLI counts without a restart.

The users file is operator data: damaged at start, it raises StateFileError (the server
and the CLI refuse to run); damaged while the server runs, the server keeps the users it
last read, alerts the owner and refuses account changes until the file is repaired. The
sessions file is runtime state: damaged, it is kept as a backup and everyone logs in again.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.state_file import StateFile, StateFileError, expect_list, print_incidents

REPO_ROOT = Path(__file__).resolve().parents[1]

ROLES = ("admin", "member")
SESSION_DAYS = 30
COOKIE = "kiri_session"
DEFAULT_PROXIES = ["172.29.70.10"]  # Caddy's fixed address on the docker network `edge`
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-぀-ヿ㐀-䶿一-鿿]{2,32}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 1024

# scrypt cost: 16 MiB of memory, ~50 ms per hash.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --- passwords -------------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    """'scrypt$n$r$p$salt$hash' (base64 salt and hash)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # for unknown names: same work as a real check


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(422, f"密码至少 {MIN_PASSWORD} 位")
    if len(password) > MAX_PASSWORD:
        raise AuthError(422, "密码太长")


# --- users -----------------------------------------------------------------------------

def public_user(u: dict[str, Any]) -> dict[str, Any]:
    """A user without the password hash."""
    return {"username": u["username"], "role": u["role"], "disabled": bool(u.get("disabled")),
            "created_at": u.get("created_at"), "streamers": streamers_of(u)}


def streamers_of(u: dict[str, Any]) -> str | list[str]:
    """Whom a user may see: "all", or a list of streamer names (members only; a new
    member sees nobody until an admin picks). Admins always see everyone."""
    if u.get("role") == "admin":
        return "all"
    s = u.get("streamers")
    return "all" if s == "all" else list(s or [])


def decode_users(raw: Any) -> list[dict[str, Any]]:
    """The file's accounts; raises on anything that isn't a list of them."""
    for u in expect_list(raw, "users"):
        if not (isinstance(u, dict) and all(isinstance(u.get(k), str) for k in ("username", "role", "password_hash"))):
            raise ValueError("not a user (needs username, role and password_hash)")
    return raw


class UserStore:
    """data/users.json: [{username, role, password_hash, disabled, created_at, streamers}].

    Re-read whenever the file changes on disk (the CLI writes it too). Damaged when this
    store is built: StateFileError. Damaged later: the users last read stay in effect,
    the incident is logged and reported once, and changes are refused until it is fixed."""

    def __init__(self, path: Path):
        self.file: StateFile[list[dict[str, Any]]] = StateFile(path, default=list, policy="strict",
                                                                decode=decode_users)
        self._users = self.file.load()

    @property
    def users(self) -> list[dict[str, Any]]:
        self.refresh()
        return self._users

    def refresh(self) -> None:
        """Re-read the file if it changed on disk. Damaged: the users read before stay (the
        state file logs and reports it once until it is repaired)."""
        self._users = self.file.reload(self._users)

    def _save(self) -> None:
        self.file.save(self._users)

    def _check_writable(self) -> None:
        """Never overwrite a damaged users file: a person repairs it (or restores a backup)."""
        self.refresh()
        if self.file.damaged is not None:
            raise AuthError(503, f"{self.file.path.name} 已损坏，修复之前不能修改账号")

    def __bool__(self) -> bool:
        return bool(self.users)

    def get(self, name: str) -> dict[str, Any] | None:
        key = name.casefold()
        return next((u for u in self.users if u["username"].casefold() == key), None)

    def _need(self, name: str) -> dict[str, Any]:
        u = self.get(name)
        if u is None:
            raise AuthError(404, "没有这个用户")
        return u

    def add(self, name: str, password: str, role: str) -> dict[str, Any]:
        name = name.strip()
        if not USERNAME_RE.match(name):
            raise AuthError(422, "用户名 2-32 位，只能用字母、数字、_ - . 或中日文")
        if role not in ROLES:
            raise AuthError(422, "角色只能是 admin 或 member")
        check_password_rules(password)
        self._check_writable()
        if self.get(name):
            raise AuthError(409, "用户名已存在")
        u = {"username": name, "role": role, "password_hash": hash_password(password), "disabled": False,
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.users.append(u)
        self._save()
        return u

    def update(self, name: str, role: str | None = None, password: str | None = None,
               disabled: bool | None = None, streamers: str | list[str] | None = None) -> dict[str, Any]:
        self._check_writable()
        u = self._need(name)
        if role is not None and role not in ROLES:
            raise AuthError(422, "角色只能是 admin 或 member")
        if password is not None:
            check_password_rules(password)
            u["password_hash"] = hash_password(password)
        if role is not None:
            u["role"] = role
        if disabled is not None:
            u["disabled"] = disabled
        if streamers is not None:
            u["streamers"] = streamers
        self._save()
        return u

    def delete(self, name: str) -> None:
        self._check_writable()
        u = self._need(name)
        self.users.remove(u)
        self._save()

    def authenticate(self, name: str, password: str) -> dict[str, Any] | None:
        """The user, if the name and password are right and the account is enabled."""
        u = self.get(name)
        ok = verify_password(password, u["password_hash"] if u else _DUMMY_HASH)
        return u if ok and u and not u.get("disabled") else None

    def active(self, name: str) -> dict[str, Any] | None:
        u = self.get(name)
        return u if u and not u.get("disabled") else None


# --- sessions --------------------------------------------------------------------------

def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def decode_sessions(raw: Any) -> dict[str, dict[str, Any]]:
    """The file's sessions; raises on anything that isn't a map of them."""
    if not isinstance(raw, dict):
        raise ValueError(f"expected a map of sessions, got {type(raw).__name__}")
    for s in raw.values():
        if not (isinstance(s, dict) and isinstance(s.get("username"), str)
                and isinstance(s.get("expires"), (int, float)) and not isinstance(s.get("expires"), bool)):
            raise ValueError("not a session (needs username and expires)")
    return raw


class SessionStore:
    """data/sessions.json: {sha256(token): {username, expires}}. The token itself is only in the cookie.

    Re-read whenever the file changes on disk (the CLI revokes sessions too). Runtime
    state: a damaged file is kept as a backup (the owner is alerted) and everyone logs in again."""

    def __init__(self, path: Path, days: int = SESSION_DAYS, clock: Callable[[], float] = time.time):
        self.file: StateFile[dict[str, dict[str, Any]]] = StateFile(path, default=dict, policy="recover",
                                                                     decode=decode_sessions)
        self._sessions = self.file.load()
        self.ttl = days * 86400
        self.clock = clock

    def _load(self) -> dict[str, dict[str, Any]]:
        self._sessions = self.file.reload(self._sessions)
        return self._sessions

    def _save(self) -> None:
        self.file.save(self._sessions)

    def create(self, username: str) -> str:
        now = self.clock()
        sessions = self._load()
        for k in [k for k, s in sessions.items() if s["expires"] <= now]:
            del sessions[k]
        token = secrets.token_urlsafe(32)
        sessions[_token_key(token)] = {"username": username, "expires": now + self.ttl}
        self._save()
        return token

    def username(self, token: str | None) -> str | None:
        if not token:
            return None
        s = self._load().get(_token_key(token))
        return s["username"] if s and s["expires"] > self.clock() else None

    def delete(self, token: str | None) -> None:
        if token and self._load().pop(_token_key(token), None):
            self._save()

    def revoke_user(self, username: str, keep: str | None = None) -> None:
        """End every session of `username` (except the one whose token is `keep`)."""
        sessions = self._load()
        key, keep_key = username.casefold(), _token_key(keep) if keep else None
        gone = [k for k, s in sessions.items() if s["username"].casefold() == key and k != keep_key]
        for k in gone:
            del sessions[k]
        if gone:
            self._save()


# --- login rate limit ------------------------------------------------------------------

class LoginLimiter:
    """`limit` failed logins from one IP within `window` seconds -> locked for `lockout` seconds."""

    def __init__(self, limit: int = 5, window: float = 900, lockout: float = 900,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.lockout, self.clock = limit, window, lockout, clock
        self.failures: dict[str, list[float]] = {}
        self.locked: dict[str, float] = {}

    def blocked(self, ip: str) -> bool:
        until = self.locked.get(ip)
        if until is None:
            return False
        if until > self.clock():
            return True
        del self.locked[ip]
        return False

    def fail(self, ip: str) -> None:
        now = self.clock()
        if len(self.failures) > 10_000:  # forget stale entries now and then
            self.failures = {k: v for k, v in self.failures.items() if v and v[-1] > now - self.window}
        recent = [t for t in self.failures.get(ip, []) if t > now - self.window] + [now]
        if len(recent) >= self.limit:
            self.locked[ip] = now + self.lockout
            self.failures.pop(ip, None)
        else:
            self.failures[ip] = recent

    def succeed(self, ip: str) -> None:
        self.failures.pop(ip, None)


def is_trusted_proxy(peer: str | None, trusted: list[str]) -> bool:
    return _in_networks(peer or "", trusted)


def client_ip(peer: str | None, forwarded_for: str | None, trusted: list[str]) -> str:
    """The client's IP: X-Forwarded-For's first address when the direct peer is a trusted proxy."""
    peer = peer or ""
    if forwarded_for and is_trusted_proxy(peer, trusted):
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return peer


def _in_networks(ip: str, networks: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


# --- everything together ---------------------------------------------------------------

@dataclass
class Auth:
    users: UserStore
    sessions: SessionStore
    trusted_proxies: list[str] = field(default_factory=lambda: list(DEFAULT_PROXIES))
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    fail_delay: float = 0.5  # seconds added to every failed login

    def user_for(self, token: str | None) -> dict[str, Any] | None:
        """The (enabled) user that owns this session token."""
        name = self.sessions.username(token)
        return self.users.active(name) if name else None


def _resolve(config: Path, value: str | None, default: str) -> Path:
    path = Path(value or default)
    return path if path.is_absolute() else config.parent / path


def auth_section(config: Path) -> dict[str, Any]:
    try:
        return json.loads(config.read_text(encoding="utf-8")).get("auth") or {}
    except FileNotFoundError:
        return {}


def load_auth(config: Path) -> Auth:
    """The stores named in the config's `auth` section (whether or not it is enabled)."""
    cfg = auth_section(config)
    return Auth(
        users=UserStore(_resolve(config, cfg.get("users_file"), "data/users.json")),
        sessions=SessionStore(_resolve(config, cfg.get("sessions_file"), "data/sessions.json")),
        trusted_proxies=list(cfg.get("trusted_proxies") or DEFAULT_PROXIES),
    )


def auth_enabled(config: Path) -> bool:
    return bool(auth_section(config).get("enabled"))


# --- CLI -------------------------------------------------------------------------------

def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    pw = getpass.getpass("密码: ")
    if pw != getpass.getpass("再输入一次: "):
        raise AuthError(1, "两次输入的密码不一样")
    return pw


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m backend.auth", description="Manage dashboard accounts.")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user", help="create an account")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true", help="admin (default: member)")
    a.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    sub.add_parser("list", help="list accounts")
    pw = sub.add_parser("passwd", help="set a new password (logs the user out everywhere)")
    pw.add_argument("name")
    pw.add_argument("--password-stdin", action="store_true")
    d = sub.add_parser("disable", help="block an account (logs it out)")
    d.add_argument("name")
    e = sub.add_parser("enable", help="unblock an account")
    e.add_argument("name")
    args = p.parse_args(argv)
    print_incidents()  # a damaged sessions file: tell the owner at the terminal

    try:
        auth = load_auth(args.config)
    except StateFileError as err:
        print(err.message, file=sys.stderr)
        return 1
    try:
        if args.cmd == "add-user":
            if auth.users.get(args.name):
                raise AuthError(409, "用户名已存在")
            u = auth.users.add(args.name, _read_password(args.password_stdin), "admin" if args.admin else "member")
            print(f"已创建 {u['username']} ({u['role']})")
            if not auth_enabled(args.config):
                print(f'注意: {args.config} 里 auth.enabled 不是 true，网页登录还没开启')
        elif args.cmd == "list":
            for u in auth.users.users:
                print(f"{u['username']:<20} {u['role']:<7} {'已停用' if u.get('disabled') else ''}")
        elif args.cmd == "passwd":
            auth.users._need(args.name)
            auth.users.update(args.name, password=_read_password(args.password_stdin))
            auth.sessions.revoke_user(args.name)
            print(f"已修改 {args.name} 的密码")
        elif args.cmd == "disable":
            auth.users.update(args.name, disabled=True)
            auth.sessions.revoke_user(args.name)
            print(f"已停用 {args.name}")
        elif args.cmd == "enable":
            auth.users.update(args.name, disabled=False)
            print(f"已启用 {args.name}")
    except AuthError as err:
        print(f"错误: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
