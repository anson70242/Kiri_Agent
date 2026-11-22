"""Short download links: the 全部下载 address a message carries is short and downloads its
one archive's zip without logging in, until it expires. Through the app with login on and
the links "全部处理完成" is sent with."""

from __future__ import annotations

import io
import json
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.monitor.server import create_app
from backend.notify import EventKind

from .helpers import A, B
from .test_auth import ORIGIN, cli, login, write_config
from .test_drive_stage import downloaded

SITE = "http://testserver"
DAY = 86400
FOLDER = "A/20261110/歌枠"  # the folder reference of A's archive in these tests
SHORT = re.compile(r"/d/[0-9A-Za-z]{10}")


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


def service(tmp_path, monkeypatch=None, auth: bool = True) -> tuple[TestClient, list]:
    """The service with automatic download for A and B (the first call also writes its config
    and, with login, its accounts); what its pipeline sends is collected."""
    cfg = tmp_path / "config.json"
    if monkeypatch:
        body = json.loads(write_config(tmp_path, auth=auth).read_text(encoding="utf-8"))
        body["monitor"]["streamers"] = [
            {"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True},
            {"name": "B", "platform": "twitch", "channel": "b", "auto_download": True}]
        cfg.write_text(json.dumps(body), encoding="utf-8")
        if auth:
            assert cli(monkeypatch, cfg, "add-user", "boss", "--admin", "--password-stdin") == 0
            assert cli(monkeypatch, cfg, "add-user", "fan", "--password-stdin") == 0
    client = TestClient(create_app(cfg), headers=ORIGIN)
    sent: list = []
    ad = client.app.state.auto
    ad.notify, ad.site_url = sent.append, SITE
    return client, sent


@pytest.fixture
def running(tmp_path, monkeypatch):
    client, sent = service(tmp_path, monkeypatch)
    client.app.state.auth.download_links.clock = Clock()
    return client, sent


def clock(client) -> Clock:
    return client.app.state.auth.download_links.clock


def ready_address(running, streamer=A, video=b"x") -> str:
    """A live of `streamer` is processed: the 全部下载 address its "全部处理完成" carries, as a path."""
    client, sent = running
    before = len(sent)
    downloaded(client.app.state.auto, streamer, video)
    (ready,) = [e for e in sent[before:] if e.kind is EventKind.READY]
    assert ready.links[0].label.startswith("全部下载")
    return ready.links[0].url.removeprefix(SITE)


def test_ready_carries_a_short_address_that_downloads_the_zip_without_logging_in(running):
    client, _ = running
    address = ready_address(running)

    assert SHORT.fullmatch(address)
    r = client.get(address)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(r.content)).read("v.mp4") == b"x"
    assert "kiri_session" not in client.cookies


def test_the_address_works_for_31_days_and_no_longer(running):
    client, _ = running
    address = ready_address(running)

    clock(client).t += 31 * DAY - 60
    assert client.get(address).status_code == 200
    clock(client).t += 120
    late = client.get(address)
    assert late.status_code == 404 and "过期" in late.text


def test_a_code_nobody_was_given_downloads_nothing(running):
    client, _ = running
    address = ready_address(running)

    for other in (address[:-1] + ("a" if address[-1] != "a" else "b"), address + "0", address[:-1], "/d/0000000000"):
        assert client.get(other).status_code == 404, other
    assert client.post(address, follow_redirects=False).status_code == 303  # only reading it is open: to the login


def zipped_video(response) -> bytes:
    return zipfile.ZipFile(io.BytesIO(response.content)).read("v.mp4")


def test_each_address_downloads_its_own_archive(running):
    client, _ = running
    a, b = ready_address(running, A, video=b"the video of A"), ready_address(running, B, video=b"the video of B")

    assert a != b
    assert zipped_video(client.get(a)) == b"the video of A"
    assert zipped_video(client.get(b)) == b"the video of B"


def test_nothing_but_the_address_itself_opens_without_logging_in(running):
    client, _ = running
    code = ready_address(running).removeprefix("/d/")

    for path, params in (("/api/archives", {}), ("/api/archives/zip", {"folder": FOLDER}),
                         ("/api/archives/zip", {"folder": FOLDER, "code": code}), ("/api/status", {"code": code})):
        assert client.get(path, params=params).status_code == 401, path
    page = client.get("/archives", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")


def test_a_member_who_is_not_shown_the_streamer_downloads_with_the_address_too(running):
    client, _ = running
    address = ready_address(running)
    login(client, "fan")  # a new member is shown nobody

    assert client.get("/api/archives/zip", params={"folder": FOLDER}).status_code == 404
    assert client.get(address).status_code == 200


def test_the_address_of_an_archive_that_is_gone_says_so(running, tmp_path):
    client, _ = running
    address = ready_address(running)
    for f in sorted((tmp_path / "dl").rglob("*"), reverse=True):
        f.unlink() if f.is_file() else f.rmdir()

    gone = client.get(address)
    assert gone.status_code == 404 and "不在服务器" in gone.text


# ---- the links file ----

def links_file(tmp_path):
    return tmp_path / "data" / "download_links.json"


def test_an_address_still_works_after_a_restart(tmp_path, monkeypatch):
    address = ready_address(service(tmp_path, monkeypatch))

    again, _ = service(tmp_path)
    assert again.get(address).status_code == 200


def test_deleting_the_links_file_cancels_the_addresses_at_once_and_new_ones_work(tmp_path, monkeypatch):
    running = service(tmp_path, monkeypatch)
    client, _ = running
    old = ready_address(running, A)
    links_file(tmp_path).unlink()

    assert client.get(old).status_code == 404
    assert client.get(ready_address(running, B)).status_code == 200


def test_a_link_made_by_another_process_on_the_same_files_works_in_the_running_service(tmp_path, monkeypatch):
    running = service(tmp_path, monkeypatch)
    other = service(tmp_path)  # started before any link was made

    assert running[0].get(ready_address(other, A)).status_code == 200
    assert other[0].get(ready_address(running, B)).status_code == 200  # and the other way round


def test_the_links_file_holds_no_code_and_forgets_expired_links(running, tmp_path):
    client, _ = running
    code = ready_address(running, A).removeprefix("/d/")

    assert code not in links_file(tmp_path).read_text(encoding="utf-8")
    assert len(json.loads(links_file(tmp_path).read_text(encoding="utf-8"))["links"]) == 1
    clock(client).t += 32 * DAY
    ready_address(running, B)
    assert len(json.loads(links_file(tmp_path).read_text(encoding="utf-8"))["links"]) == 1  # A's is gone


def test_there_is_no_links_file_before_the_first_link(running, tmp_path):
    client, _ = running

    assert client.get("/d/0000000000").status_code == 404
    assert not links_file(tmp_path).exists()


def test_without_login_on_the_dashboard_the_address_is_the_plain_zip_address(tmp_path, monkeypatch):
    running = service(tmp_path, monkeypatch, auth=False)
    address = ready_address(running)

    assert address == "/api/archives/zip?folder=A%2F20261110%2F%E6%AD%8C%E6%9E%A0"
    assert running[0].get(address).status_code == 200
    assert running[0].get("/d/0000000000").status_code == 404


# ---- the signed addresses of the first messages (2026-11-22), accepted until they expire ----

# What that version signed for FOLDER with the key bytes(range(32)), 31 days before EXPIRES.
KEY_FILE = '{"key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}'
EXPIRES = 1798030543
SIGNED = ("/api/archives/zip?folder=A%2F20261110%2F%E6%AD%8C%E6%9E%A0&expires=1798030543"
          "&sig=DXnt%2FnLVLK32h4wRkFz7DfSS3VwMLKQ1EVVgF%2B%2FiuRQ%3D")


def test_a_signed_address_still_downloads_until_it_expires_beside_the_short_ones(tmp_path, monkeypatch):
    links_file(tmp_path).parent.mkdir()
    links_file(tmp_path).write_text(KEY_FILE, encoding="utf-8")  # as that version left the file
    running = service(tmp_path, monkeypatch)
    client, _ = running
    client.app.state.auth.download_links.clock = now = Clock()
    now.t = EXPIRES - 60
    short = ready_address(running)  # makes the archive, and the first short link in that file

    assert client.get(SIGNED).status_code == 200 and client.get(short).status_code == 200
    assert client.get(SIGNED.replace("sig=DX", "sig=XD")).status_code == 401
    assert client.get(SIGNED.replace("%E6%9E%A0", "%E6%9E%A0x")).status_code == 401
    now.t = EXPIRES + 60
    assert client.get(SIGNED).status_code == 401 and client.get(short).status_code == 200
