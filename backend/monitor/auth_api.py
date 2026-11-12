"""Login for the dashboard: who may do what (`required_role`), the gate in front of every
request (`install_auth`), and the endpoints /login, /api/login, /api/logout, /api/me, /api/users.

Roles: `admin` may do everything. `member` may look at and download everything except
the 通知 page, and may not change anything (the one write a member may do is
POST /api/refresh, which only re-polls live status).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from backend.auth import (COOKIE, SESSION_DAYS, Auth, AuthError, client_ip, is_trusted_proxy, public_user,
                          streamers_of)

from .__main__ import REPO_ROOT

LOGIN_PAGE = REPO_ROOT / "frontend" / "login.html"

SAFE_METHODS = {"GET", "HEAD"}
HEALTH = "/healthz"
PUBLIC = {("GET", "/login"), ("HEAD", "/login"), ("POST", "/api/login"), ("POST", "/api/logout")}
ADMIN_ONLY_PREFIXES = ("/notify", "/api/notify", "/users", "/api/users")
MEMBER_WRITES = {("POST", "/api/refresh")}

NO_USERS = ("还没有任何账号。请在服务器上创建第一个管理员: "
            "uv run -m backend.auth add-user <名字> --admin"
            "（docker: docker compose exec kiri /app/.venv/bin/python -m backend.auth add-user <名字> --admin）")


def required_role(method: str, path: str) -> str | None:
    """The role a request needs: None (anyone, logged in or not), "member" (any logged-in
    user) or "admin". The single place that decides access."""
    method = method.upper()
    if (method, path) in PUBLIC:
        return None
    if any(path == p or path.startswith(p + "/") for p in ADMIN_ONLY_PREFIXES):
        return "admin"  # the 通知 and 用户 pages and their APIs: even reading them
    if method in SAFE_METHODS or (method, path) in MEMBER_WRITES:
        return "member"
    return "admin"  # every other change


def allowed(role: str, method: str, path: str) -> bool:
    need = required_role(method, path)
    return need is None or role == "admin" or need == role


def is_api(path: str) -> bool:
    return path.startswith("/api/")


def proxied(request: Request, header: str, trusted: list[str]) -> str:
    """An X-Forwarded-* header's first value, only when the direct peer is a trusted proxy."""
    peer = request.client.host if request.client else None
    if not is_trusted_proxy(peer, trusted):
        return ""
    return request.headers.get(header, "").split(",")[0].strip()


def same_origin(request: Request, trusted: list[str]) -> bool:
    """CSRF check for writes: Origin (or Referer) must name this site's own host."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return False
    host = (proxied(request, "x-forwarded-host", trusted) or request.headers.get("host") or "").lower()
    return bool(host) and urlsplit(source).netloc.lower() == host


def is_https(request: Request, trusted: list[str]) -> bool:
    """The scheme the browser used: X-Forwarded-Proto from the proxy, else the request's own."""
    scheme = proxied(request, "x-forwarded-proto", trusted).lower() or request.url.scheme
    return scheme == "https"


def _page(status: int, title: str, text: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · 切片小助手</title>'
        f'<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;'
        f'background:linear-gradient(160deg,#eef5ff 0%,#f3faff 50%,#f5f7ff 100%);color:#172554;'
        f'font:15px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}'
        f'main{{max-width:560px;margin:16px;background:#fff;border:1px solid #c7dbf7;border-radius:12px;'
        f'padding:24px;box-shadow:0 8px 20px rgba(37,99,235,.10)}}a{{color:#2563eb}}code{{word-break:break-all}}</style>'
        f'</head><body><main><h1 style="font-size:20px;margin:0 0 8px">{title}</h1><p>{text}</p>'
        f'<p><a href="/">返回首页</a></p></main></body></html>',
        status_code=status,
    )


def _deny(request: Request, status: int, detail: str) -> Response:
    if is_api(request.url.path):
        return JSONResponse({"detail": detail}, status_code=status)
    return _page(status, {403: "没有权限", 503: "需要先创建管理员"}.get(status, "出错了"), detail)


def install_auth(app: FastAPI, auth: Auth, streamer_names: Callable[[], set[str]] = set) -> None:
    """Put the login gate in front of every route and add the login endpoints."""
    app.state.auth = auth

    @app.middleware("http")
    async def gate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        method, path = request.method.upper(), request.url.path
        if path == HEALTH and method in SAFE_METHODS:  # Docker / CI: "is it up?", no data
            return await call_next(request)
        if not auth.users:  # never leave the site open just because nobody has an account yet
            return _deny(request, 503, NO_USERS)
        if method not in SAFE_METHODS and not same_origin(request, auth.trusted_proxies):
            return _deny(request, 403, "跨站请求被拒绝")
        need = required_role(method, path)
        if need is None:
            return await call_next(request)
        user = auth.user_for(request.cookies.get(COOKIE))
        if user is None:
            if is_api(path):
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        if not allowed(user["role"], method, path):
            return _deny(request, 403, "没有权限")
        request.state.user = user
        return await call_next(request)

    app.include_router(create_auth_router(auth, streamer_names))


class LoginBody(BaseModel):
    username: str
    password: str


class NewUser(BaseModel):
    username: str
    password: str
    role: Literal["admin", "member"] = "member"


class UserChange(BaseModel):
    role: Literal["admin", "member"] | None = None
    password: str | None = None
    disabled: bool | None = None
    streamers: Literal["all"] | list[str] | None = None  # whom a member sees: "all" or names


def visible_streamers(request: Request, names: set[str]) -> set[str] | None:
    """The streamer names a request may see (`names`: everyone in the streamer list), or
    None for everyone: login off, admins, and members allowed "all". A member's picked
    names only count while those streamers are still in the list, so archives of a
    deleted streamer stay with "all" members."""
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    allowed = streamers_of(user)
    return None if allowed == "all" else set(allowed) & names


def is_self(request: Request, user: dict) -> bool:
    return user["username"].casefold() == request.state.user["username"].casefold()


def create_auth_router(auth: Auth, streamer_names: Callable[[], set[str]] = set) -> APIRouter:
    router = APIRouter()

    def fail(e: AuthError) -> HTTPException:
        return HTTPException(e.status, str(e))

    @router.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(LOGIN_PAGE)

    @router.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict:
        ip = client_ip(request.client.host if request.client else None,
                       request.headers.get("x-forwarded-for"), auth.trusted_proxies)
        if auth.limiter.blocked(ip):
            raise HTTPException(429, "登录失败次数太多，请 15 分钟后再试")
        user = auth.users.authenticate(body.username, body.password)
        if user is None:
            auth.limiter.fail(ip)
            if auth.fail_delay:
                await asyncio.sleep(auth.fail_delay)
            raise HTTPException(401, "用户名或密码错误")
        auth.limiter.succeed(ip)
        token = auth.sessions.create(user["username"])
        response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/", httponly=True,
                            samesite="lax", secure=is_https(request, auth.trusted_proxies))
        return {"username": user["username"], "role": user["role"]}

    @router.post("/api/logout")
    async def logout(request: Request, response: Response) -> dict:
        auth.sessions.delete(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=is_https(request, auth.trusted_proxies))
        return {"ok": True}

    @router.get("/api/me")
    async def me(request: Request) -> dict:
        u = request.state.user
        return {"username": u["username"], "role": u["role"], "auth": True}

    @router.get("/api/users")
    async def list_users() -> dict:
        return {"users": [public_user(u) for u in auth.users.users]}

    @router.post("/api/users", status_code=201)
    async def add_user(body: NewUser) -> dict:
        try:
            return public_user(auth.users.add(body.username, body.password, body.role))
        except AuthError as e:
            raise fail(e) from None

    @router.patch("/api/users")
    async def change_user(name: str, body: UserChange, request: Request) -> dict:
        target = auth.users.get(name)
        if target is None:
            raise HTTPException(404, "没有这个用户")
        is_me = is_self(request, target)
        if is_me and (body.role == "member" or body.disabled):
            raise HTTPException(400, "不能停用或降级自己")
        if isinstance(body.streamers, list) and not set(body.streamers) <= streamer_names():
            raise HTTPException(422, "名单里没有这个主播")
        try:
            u = auth.users.update(name, **body.model_dump())
        except AuthError as e:
            raise fail(e) from None
        if body.password is not None or body.disabled:
            # a new password or a disabled account logs out everywhere (your own change keeps this session)
            auth.sessions.revoke_user(u["username"], keep=request.cookies.get(COOKIE) if is_me else None)
        return public_user(u)

    @router.delete("/api/users")
    async def delete_user(name: str, request: Request) -> dict:
        target = auth.users.get(name)
        if target is None:
            raise HTTPException(404, "没有这个用户")
        if is_self(request, target):
            raise HTTPException(400, "不能删除自己")
        try:
            auth.users.delete(name)
        except AuthError as e:
            raise fail(e) from None
        auth.sessions.revoke_user(target["username"])
        return {"username": target["username"]}

    return router


def install_open_me(app: FastAPI) -> None:
    """Login off (no `auth.enabled`, or --dev): /api/me says everyone is an admin."""

    @app.get("/api/me")
    async def me() -> dict:
        return {"username": None, "role": "admin", "auth": False}
