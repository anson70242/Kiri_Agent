"""Which streamers a member may see (per person, set on the 用户 page by an admin).

New members see nobody until an admin picks streamers or "all". The limit is enforced by
the server, not just hidden in the page: archives of other streamers 404 even by URL.
Archives of streamers no longer in the list (deleted) are seen only by "all" members.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.monitor.server import create_app

from .helpers import bare_archive
from .test_auth import ORIGIN, cli, login

FOLDERS = {"A": "A/20261110/歌枠", "B": "B/20261109/雑談", "Old": "Old/20261001/昔"}


def write_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "auth": {"enabled": True},
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a"},
                                  {"name": "A", "platform": "twitch", "channel": "a_tw"},  # same person
                                  {"name": "B", "platform": "twitch", "channel": "b"}],
                    "streamers_file": str(tmp_path / "data" / "streamers.json")},
        "notify": {"discord": {"enabled": False}},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")}},
    }), encoding="utf-8")
    for folder in FOLDERS.values():  # an archive per streamer, "Old" one no longer in the list
        bare_archive(tmp_path / "dl", *folder.split("/"), stem="v", video=b"video", highlights={"rows": []})
    return cfg


@pytest.fixture
def site(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    assert cli(monkeypatch, cfg, "add-user", "boss", "--admin", "--password-stdin") == 0
    assert cli(monkeypatch, cfg, "add-user", "fan", "--password-stdin") == 0
    app = create_app(cfg)
    app.state.auth.fail_delay = 0
    admin = TestClient(app, headers=ORIGIN)
    member = TestClient(app, headers=ORIGIN)
    login(admin, "boss")
    login(member, "fan")
    return admin, member


def creators(client) -> set[str]:
    return {a["creator"] for a in client.get("/api/archives").json()["archives"]}


def names(client) -> set[str]:
    return {s["name"] for s in client.get("/api/status").json()["streamers"]}


def can_open(client, who: str) -> bool:
    r = client.get("/api/archives/file", params={"folder": FOLDERS[who], "name": "v.mp4"})
    assert r.status_code in (200, 404), r.status_code
    return r.status_code == 200


def allow(admin, value):
    return admin.patch("/api/users", params={"name": "fan"}, json={"streamers": value})


def test_a_new_member_sees_nobody(site):
    _, member = site
    assert names(member) == set() and creators(member) == set()
    assert member.get("/api/highlights").json()["tables"] == []
    assert not can_open(member, "A")


def test_a_member_sees_only_the_people_picked_on_every_page(site, monkeypatch):
    async def no_network(self):  # the refresh itself polls YouTube / Twitch
        return []

    monkeypatch.setattr("backend.monitor.base.StreamMonitor.refresh", no_network)
    admin, member = site
    r = allow(admin, ["A"])
    assert r.status_code == 200 and r.json()["streamers"] == ["A"]

    assert names(member) == {"A"}  # both of A's channels
    assert {s["name"] for s in member.post("/api/refresh").json()["streamers"]} <= {"A"}  # members may refresh
    assert len(member.get("/api/status").json()["streamers"]) == 2
    assert {s["name"] for s in member.get("/api/streamers").json()["streamers"]} == {"A"}
    assert creators(member) == {"A"}
    assert {t["creator"] for t in member.get("/api/highlights").json()["tables"]} == {"A"}
    assert can_open(member, "A")
    # Other people's archives can't be reached even by URL.
    assert not can_open(member, "B")
    for path in ("/api/archives/zip", "/api/archives/chat", "/api/highlights/table", "/api/highlights/xlsx",
                 "/api/archives/transcript"):
        assert member.get(path, params={"folder": FOLDERS["B"]}).status_code == 404, path


def test_all_means_everyone_even_streamers_deleted_since(site):
    admin, member = site
    assert allow(admin, "all").status_code == 200
    assert names(member) == {"A", "B"}
    assert creators(member) == {"A", "B", "Old"}
    assert can_open(member, "Old")


def test_a_picked_list_never_reaches_deleted_streamers(site):
    admin, member = site
    assert allow(admin, ["A", "B"]).status_code == 200
    assert creators(member) == {"A", "B"}
    assert not can_open(member, "Old")
    assert admin.delete("/api/streamers", params={"id": "twitch:b"}).status_code == 200  # B deleted
    assert creators(member) == {"A"} and not can_open(member, "B")


def test_admins_always_see_everyone(site):
    admin, _ = site
    assert creators(admin) == {"A", "B", "Old"} and can_open(admin, "Old")


def test_the_choice_is_checked_and_listed(site):
    admin, member = site
    assert allow(admin, ["Nobody"]).status_code == 422
    assert allow(admin, "some").status_code == 422
    assert allow(admin, ["B"]).status_code == 200
    fan = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "fan")
    assert fan["streamers"] == ["B"]
    boss = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "boss")
    assert boss["streamers"] == "all"  # admins: everyone, whatever is stored
    assert allow(member, "all").status_code == 403  # members can't change it themselves
