"""/api/notify: the 通知 page — channel status, QQ groups and their subscriptions, history."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.monitor.server import create_app

from .test_qq import SECRET


def write_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a"},
                                  {"name": "A", "platform": "twitch", "channel": "a_tw"},
                                  {"name": "B", "platform": "twitch", "channel": "b"}],
                    "streamers_file": str(tmp_path / "data" / "streamers.json")},
        "notify": {"discord": {"webhook_env": "KIRI_T_HOOK"}, "state_file": str(tmp_path / "state.json"),
                   "log_file": str(tmp_path / "log.json"),
                   "qq": {"app_id_env": "KIRI_T_QQ_ID", "secret_env": "KIRI_T_QQ_SECRET",
                          "groups_file": str(tmp_path / "qq_groups.json")}},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")}},
    }), encoding="utf-8")
    return cfg


def app(tmp_path, monkeypatch, qq=True):
    monkeypatch.setenv("KIRI_T_HOOK", "https://discord.test/hook")
    if qq:
        monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
        monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    client = TestClient(create_app(write_config(tmp_path)))
    if qq:
        client.app.state.notifier.qq.groups.register("G1")  # as if the bot was added to a group
    return client


def test_status_lists_channels_groups_and_choices(tmp_path, monkeypatch):
    body = app(tmp_path, monkeypatch).get("/api/notify").json()
    assert body["discord"]["configured"] is True
    assert body["qq"]["configured"] is True and body["qq"]["online"] is False
    (g,) = body["groups"]
    assert g["openid"] == "G1" and g["enabled"] is False and g["kinds"] == ["live", "offline", "ready", "uploaded"]
    assert [s["id"] for s in body["streamers"]] == ["A", "B"]  # one per person, not per channel
    assert {"id": "live", "label": "开播"} in body["kinds"] and len(body["kinds"]) == 12
    assert {"id": "ready", "label": "全部处理完成"} in body["kinds"]
    assert body["log"] == []


def test_a_group_is_named_subscribed_and_switched_on(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    r = client.patch("/api/notify/groups", params={"id": "G1"}, json={
        "name": "章鱼烧字幕组", "enabled": True, "streamers": ["A"], "kinds": ["live", "analyzed"]})
    assert r.status_code == 200
    (g,) = client.get("/api/notify").json()["groups"]
    assert (g["name"], g["enabled"], g["streamers"], g["kinds"]) == ("章鱼烧字幕组", True, ["A"], ["live", "analyzed"])
    (g,) = json.loads((tmp_path / "qq_groups.json").read_text(encoding="utf-8"))  # kept for restarts
    assert g["name"] == "章鱼烧字幕组"


def test_bad_group_settings_are_refused(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    patch = lambda gid, body: client.patch("/api/notify/groups", params={"id": gid}, json=body).status_code  # noqa: E731
    assert patch("NOPE", {"enabled": True}) == 404
    assert patch("G1", {"kinds": ["live", "party"]}) == 422
    assert patch("G1", {"streamers": ["Nobody"]}) == 422


def test_a_group_can_be_deleted(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    assert client.delete("/api/notify/groups", params={"id": "G1"}).status_code == 200
    assert client.get("/api/notify").json()["groups"] == []


def test_test_message_to_a_group(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    sent = []

    async def fake_send(group, content, **kw):
        sent.append((group, content))
        return {"id": "M1"}

    client.app.state.notifier.qq.api.send_markdown = fake_send
    r = client.post("/api/notify/test", json={"target": "G1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sent[0][0] == "G1" and "测试" in sent[0][1]
    (entry,) = client.get("/api/notify").json()["log"]
    assert entry["channel"] == "qq" and entry["target"] == "G1" and entry["ok"]


def test_without_qq_credentials_the_page_says_so(tmp_path, monkeypatch):
    body = app(tmp_path, monkeypatch, qq=False).get("/api/notify").json()
    assert body["qq"]["configured"] is False and body["groups"] == []



def test_groups_saved_with_channel_ids_show_as_people(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    client.app.state.notifier.qq.groups.update("G1", streamers=["twitch:a_tw", "twitch:b"])  # an older save
    (g,) = client.get("/api/notify").json()["groups"]
    assert sorted(g["streamers"]) == ["A", "B"]
