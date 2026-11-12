"""Login and roles: admin (everything) and member (view + download, no changes, no 通知 page).

Users are created with the CLI (`python -m backend.auth`), sessions are a cookie.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from backend import auth as auth_cli
from backend.monitor.server import create_app

ORIGIN = {"Origin": "http://testserver"}
CADDY = ("172.29.70.10", 4000)  # the reverse proxy's fixed address: the default trusted proxy
TAILSCALE = ("172.24.0.1", 4000)  # docker gateway: requests through Tailscale; not trusted


def write_config(tmp_path, auth=True, **auth_cfg):
    cfg = tmp_path / "config.json"
    body = {
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a"}],
                    "streamers_file": str(tmp_path / "data" / "streamers.json")},
        "notify": {"discord": {"enabled": False}},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")}},
    }
    if auth:
        body["auth"] = {"enabled": True, **auth_cfg}
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def cli(monkeypatch, cfg, *argv, password="secret-pass"):
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    return auth_cli.main(["--config", str(cfg), *argv])


def make_client(tmp_path, monkeypatch, auth_cfg=None, **kw):
    cfg = write_config(tmp_path, **(auth_cfg or {}))
    assert cli(monkeypatch, cfg, "add-user", "boss", "--admin", "--password-stdin") == 0
    assert cli(monkeypatch, cfg, "add-user", "fan", "--password-stdin") == 0
    client = TestClient(create_app(cfg), headers=ORIGIN, **kw)
    client.app.state.auth.fail_delay = 0
    return client


def login(client, name, password="secret-pass"):
    return client.post("/api/login", json={"username": name, "password": password})


@pytest.fixture
def client(tmp_path, monkeypatch):
    return make_client(tmp_path, monkeypatch)


def test_cli_creates_users_and_login_sets_a_session(client, tmp_path):
    users = json.loads((tmp_path / "data" / "users.json").read_text(encoding="utf-8"))
    assert [(u["username"], u["role"], u["disabled"]) for u in users] == [("boss", "admin", False), ("fan", "member", False)]
    assert "secret-pass" not in json.dumps(users)

    r = login(client, "boss")
    assert r.status_code == 200 and r.json() == {"username": "boss", "role": "admin"}
    cookie = r.headers["set-cookie"]
    assert "kiri_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert "Secure" not in cookie
    me = client.get("/api/me").json()
    assert (me["username"], me["role"]) == ("boss", "admin")
    token = client.cookies["kiri_session"]
    assert token not in (tmp_path / "data" / "sessions.json").read_text(encoding="utf-8")  # stored hashed


def test_not_logged_in_pages_redirect_to_login_and_apis_say_401(client):
    r = client.get("/archives", params={"x": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Farchives%3Fx%3D1"
    r = client.get("/api/status")
    assert r.status_code == 401 and r.json() == {"detail": "请先登录"}
    page = client.get("/login")
    assert page.status_code == 200 and "登录 · 切片小助手" in page.text


def test_wrong_password_and_unknown_user_look_the_same(client):
    for name, pw in [("boss", "wrong-pass"), ("nobody", "secret-pass")]:
        r = login(client, name, pw)
        assert r.status_code == 401 and r.json() == {"detail": "用户名或密码错误"}
    assert "kiri_session" not in client.cookies


def test_logout_ends_the_session(client):
    login(client, "fan")
    token = client.cookies["kiri_session"]
    assert client.post("/api/logout").status_code == 200
    client.cookies.set("kiri_session", token)  # even a kept copy of the cookie no longer works
    assert client.get("/api/me").status_code == 401


def test_five_failures_lock_the_ip_out(client):
    for _ in range(5):
        assert login(client, "boss", "wrong-pass").status_code == 401
    assert login(client, "boss").status_code == 429  # even with the right password
    assert login(client, "fan").status_code == 429


def test_lockout_uses_the_forwarded_ip_only_behind_a_trusted_proxy(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, client=CADDY)
    bad = {"username": "boss", "password": "x" * 8}
    good = {"username": "boss", "password": "secret-pass"}
    for _ in range(5):
        assert client.post("/api/login", json=bad, headers={"X-Forwarded-For": "203.0.113.9, 172.29.70.10"}).status_code == 401
    assert client.post("/api/login", json=good, headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
    assert client.post("/api/login", json=good, headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200

    for peer in [TAILSCALE, ("127.0.0.1", 4000), ("203.0.113.50", 4000)]:  # none of them is the proxy
        (tmp_path / peer[0]).mkdir()
        direct = make_client(tmp_path / peer[0], monkeypatch, client=peer)
        for i in range(5):  # a spoofed X-Forwarded-For does not change the rate-limit key
            direct.post("/api/login", json=bad, headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert direct.post("/api/login", json=good, headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429


def test_trusted_proxies_come_from_the_config(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, auth_cfg={"trusted_proxies": ["10.1.0.0/16"]}, client=("10.1.2.3", 1))
    bad = {"username": "boss", "password": "x" * 8}
    for _ in range(5):
        client.post("/api/login", json=bad, headers={"X-Forwarded-For": "203.0.113.9"})
    assert client.post("/api/login", json={"username": "boss", "password": "secret-pass"},
                       headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200


def test_member_can_view_and_download_but_not_change_anything(client, tmp_path):
    client.app.state.auth.users.update("fan", streamers="all")  # who they see: tests/test_streamer_access.py
    login(client, "fan")
    assert client.get("/api/me").json()["role"] == "member"
    for path in ["/", "/archives", "/highlights", "/api/status", "/api/streamers", "/api/archives",
                 "/api/downloads", "/api/highlights"]:
        assert client.get(path).status_code == 200, path
    folder = tmp_path / "dl" / "A" / "20261110_v1"
    folder.mkdir(parents=True)
    (folder / "v1.mp4").write_bytes(b"0123456789")
    assert client.get("/api/archives/zip", params={"folder": "A/20261110_v1"}).status_code == 200
    assert client.post("/api/refresh").status_code == 200  # re-polling is harmless

    denied = [
        client.post("/api/streamers", json={"platform": "twitch", "channel": "c"}),
        client.patch("/api/streamers", params={"id": "youtube:@a"}, json={"keep": 3}),
        client.delete("/api/streamers", params={"id": "youtube:@a"}),
        client.patch("/api/notify/groups", params={"id": "G1"}, json={"enabled": True}),
        client.get("/api/notify"),
        client.get("/api/users"),
        client.post("/api/users", json={"username": "evil", "password": "longenough", "role": "admin"}),
    ]
    for r in denied:
        assert r.status_code == 403 and r.json() == {"detail": "没有权限"}, r.request.url
    page = client.get("/notify")
    assert page.status_code == 403 and "没有权限" in page.text
    assert [s["id"] for s in client.get("/api/streamers").json()["streamers"]] == ["youtube:@a"]


def test_admin_can_change_things(client):
    login(client, "boss")
    assert client.patch("/api/streamers", params={"id": "youtube:@a"}, json={"keep": 3}).status_code == 200
    assert client.get("/notify").status_code == 200
    assert client.get("/api/notify").status_code == 200
    assert client.get("/api/users").status_code == 200


def test_writes_from_another_site_are_refused(client):
    login(client, "boss")
    patch = {"params": {"id": "youtube:@a"}, "json": {"keep": 3}}
    evil = client.patch("/api/streamers", **patch, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    bare = TestClient(client.app, cookies=dict(client.cookies))  # no Origin header at all
    assert bare.patch("/api/streamers", **patch).status_code == 403
    assert bare.post("/api/login", json={"username": "boss", "password": "secret-pass"}).status_code == 403
    assert bare.patch("/api/streamers", **patch, headers={"Referer": "http://testserver/"}).status_code == 200
    via_proxy = {"Origin": "https://203.0.113.1", "X-Forwarded-Host": "203.0.113.1"}
    caddy = TestClient(client.app, cookies=dict(client.cookies), client=CADDY)
    assert caddy.patch("/api/streamers", **patch, headers=via_proxy).status_code == 200
    assert bare.patch("/api/streamers", **patch, headers=via_proxy).status_code == 403  # not from the proxy


def test_cookie_is_secure_behind_https(client):
    body, https = {"username": "boss", "password": "secret-pass"}, {"X-Forwarded-Proto": "https"}
    caddy = TestClient(client.app, headers=ORIGIN, client=CADDY)
    r = caddy.post("/api/login", json=body, headers=https)
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]
    tailscale = TestClient(client.app, headers=ORIGIN, client=TAILSCALE)
    r = tailscale.post("/api/login", json=body, headers=https)  # header not believed: plain http to kiri
    assert r.status_code == 200 and "Secure" not in r.headers["set-cookie"]
    direct_https = TestClient(client.app, base_url="https://testserver", headers={"Origin": "https://testserver"})
    assert "Secure" in direct_https.post("/api/login", json=body).headers["set-cookie"]


def test_disabling_or_changing_password_ends_sessions(client):
    fan = TestClient(client.app, headers=ORIGIN)
    login(fan, "fan")
    login(client, "boss")
    assert client.patch("/api/users", params={"name": "fan"}, json={"password": "new-pass-123"}).status_code == 200
    assert fan.get("/api/me").status_code == 401
    assert login(fan, "fan", "new-pass-123").status_code == 200

    assert client.patch("/api/users", params={"name": "fan"}, json={"disabled": True}).status_code == 200
    assert fan.get("/api/me").status_code == 401
    assert login(fan, "fan", "new-pass-123").status_code == 401
    assert client.get("/api/me").status_code == 200  # the admin is still logged in


def test_cli_passwd_and_disable_reach_a_running_server(client, tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    login(client, "fan")
    assert cli(monkeypatch, cfg, "passwd", "fan", "--password-stdin", password="other-pass-1") == 0
    assert client.get("/api/me").status_code == 401
    assert login(client, "fan", "secret-pass").status_code == 401
    assert login(client, "fan", "other-pass-1").status_code == 200
    assert cli(monkeypatch, cfg, "disable", "fan") == 0
    assert client.get("/api/me").status_code == 401
    assert cli(monkeypatch, cfg, "add-user", "fan", "--password-stdin") == 1  # already exists
    assert cli(monkeypatch, cfg, "add-user", "x", "--password-stdin") == 1  # name too short
    assert cli(monkeypatch, cfg, "add-user", "short", "--password-stdin", password="1234567") == 1
    assert cli(monkeypatch, cfg, "passwd", "ghost", "--password-stdin") == 1


def test_user_api(client):
    login(client, "boss")
    users = client.get("/api/users").json()["users"]
    assert [(u["username"], u["role"]) for u in users] == [("boss", "admin"), ("fan", "member")]
    assert not any("password" in k for u in users for k in u)

    r = client.post("/api/users", json={"username": "新人_1", "password": "longenough", "role": "member"})
    assert r.status_code == 201 and r.json()["username"] == "新人_1" and "password_hash" not in r.json()
    assert client.post("/api/users", json={"username": "新人_1", "password": "longenough"}).status_code == 409
    assert client.post("/api/users", json={"username": "a b", "password": "longenough"}).status_code == 422
    assert client.post("/api/users", json={"username": "ok_name", "password": "short"}).status_code == 422
    assert client.post("/api/users", json={"username": "ok_name", "password": "longenough",
                                           "role": "god"}).status_code == 422
    assert client.patch("/api/users", params={"name": "新人_1"}, json={"role": "admin"}).json()["role"] == "admin"
    assert client.patch("/api/users", params={"name": "ghost"}, json={"role": "admin"}).status_code == 404

    # no self-lockout
    assert client.patch("/api/users", params={"name": "boss"}, json={"role": "member"}).status_code == 400
    assert client.patch("/api/users", params={"name": "BOSS"}, json={"disabled": True}).status_code == 400
    assert client.delete("/api/users", params={"name": "boss"}).status_code == 400
    assert client.patch("/api/users", params={"name": "boss"}, json={"password": "boss-new-pass"}).status_code == 200
    assert client.get("/api/me").status_code == 200  # changing your own password keeps you logged in

    assert client.delete("/api/users", params={"name": "新人_1"}).status_code == 200
    assert [u["username"] for u in client.get("/api/users").json()["users"]] == ["boss", "fan"]


def test_no_users_yet_means_closed_with_instructions(tmp_path):
    client = TestClient(create_app(write_config(tmp_path)))
    r = client.get("/")
    assert r.status_code == 503 and "add-user" in r.text
    assert client.get("/api/status").status_code == 503
    assert client.get("/login").status_code == 503


def test_healthz_answers_without_login_and_says_nothing_else(tmp_path, monkeypatch):
    # Docker's health check and the CI smoke test: the server is up, even with no account yet.
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
    closed = TestClient(create_app(write_config(tmp_path / "a")))
    assert closed.get("/healthz").json() == {"ok": True}
    client = make_client(tmp_path / "b", monkeypatch)
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.post("/healthz", follow_redirects=False).status_code == 303  # writes still need login


def test_auth_off_without_config_section_and_in_dev_mode(tmp_path):
    open_client = TestClient(create_app(write_config(tmp_path, auth=False)))
    assert open_client.get("/api/status").status_code == 200
    assert open_client.get("/api/me").json() == {"username": None, "role": "admin", "auth": False}
    dev = TestClient(create_app(write_config(tmp_path), dev=True))
    assert dev.get("/api/status").status_code == 200


def test_users_page_is_admin_only_and_common_js_is_for_everyone_logged_in(client):
    assert client.get("/common.js", follow_redirects=False).status_code == 303  # logged out
    login(client, "fan")
    js = client.get("/common.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    page = client.get("/users")
    assert page.status_code == 403 and "没有权限" in page.text
    login(client, "boss")
    page = client.get("/users")
    assert page.status_code == 200 and "用户" in page.text
