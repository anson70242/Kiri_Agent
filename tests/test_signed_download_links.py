"""Signed download links: the 全部下载 address a message carries works without logging in,
for its one archive and until it expires. Through the app with login on and the links
"全部处理完成" is sent with."""

from __future__ import annotations

import io
import json
import time
import zipfile
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from backend.archive import Archive
from backend.downloader.auto import ready_links
from backend.monitor.server import create_app
from backend.notify import EventKind

from .helpers import sample_archive
from .test_auth import ORIGIN, cli, login, make_client, write_config
from .test_drive_stage import downloaded

SITE = "http://testserver"
DAY = 86400


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def client(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    client.app.state.auth.download_links.clock = Clock()
    return client


def clock(client) -> Clock:
    return client.app.state.auth.download_links.clock


def zip_address(client, tmp_path, creator="A", title="歌枠") -> str:
    """The 全部下载 address "全部处理完成" carries for a new archive of `creator`, as a path."""
    folder = sample_archive(tmp_path / "dl", creator, "20261110", title)
    link, _page = ready_links(Archive.from_folder(folder).video, SITE, tmp_path / "dl",
                              client.app.state.auth.download_links.sign)
    assert link.url.startswith(SITE + "/api/archives/zip?")
    return link.url.removeprefix(SITE)


def changed(address: str, **params: str) -> str:
    """The address with these query parameters replaced."""
    parts = urlsplit(address)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()} | params
    return f"{parts.path}?{urlencode(query)}"


def test_the_address_in_a_message_downloads_the_zip_without_logging_in(client, tmp_path):
    r = client.get(zip_address(client, tmp_path))

    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 4  # video, chat, two SRT
    assert "kiri_session" not in client.cookies


def test_without_its_signature_the_address_asks_for_the_login_as_before(client, tmp_path):
    address = zip_address(client, tmp_path)
    folder = parse_qs(urlsplit(address).query)["folder"][0]

    r = client.get("/api/archives/zip", params={"folder": folder})
    assert r.status_code == 401 and r.json() == {"detail": "请先登录"}


def test_the_address_works_for_31_days_and_no_longer(client, tmp_path):
    address = zip_address(client, tmp_path)

    clock(client).t += 31 * DAY - 60
    assert client.get(address).status_code == 200
    clock(client).t += 120
    assert client.get(address).status_code == 401


def test_a_changed_signature_or_a_later_expiry_is_refused(client, tmp_path):
    address = zip_address(client, tmp_path)
    query = parse_qs(urlsplit(address).query)
    sig, expires = query["sig"][0], query["expires"][0]

    assert client.get(changed(address, sig=sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB"))).status_code == 401
    assert client.get(changed(address, sig="")).status_code == 401
    assert client.get(changed(address, expires=str(int(expires) + 365 * DAY))).status_code == 401
    assert client.get(changed(address, expires="never")).status_code == 401


def test_the_signature_of_one_archive_opens_no_other_archive(client, tmp_path):
    address = zip_address(client, tmp_path, title="歌枠")
    sample_archive(tmp_path / "dl", "A", "20261110", "雑談")

    assert client.get(changed(address, folder="A/20261110/雑談")).status_code == 401
    assert client.get(address).status_code == 200


def test_the_signature_opens_only_the_zip(client, tmp_path):
    address = zip_address(client, tmp_path)
    query = urlsplit(address).query

    for path in ("/api/archives", "/api/archives/srt", "/api/archives/chat", "/api/archives/file", "/api/status"):
        assert client.get(f"{path}?{query}").status_code == 401, path
    page = client.get(f"/archives?{query}", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
    assert client.post(address).status_code in (401, 403, 405)


def test_a_member_who_is_not_shown_the_streamer_downloads_with_the_address_too(client, tmp_path):
    address = zip_address(client, tmp_path)
    folder = parse_qs(urlsplit(address).query)["folder"][0]
    login(client, "fan")  # a new member is shown nobody

    assert client.get("/api/archives/zip", params={"folder": folder}).status_code == 404
    assert client.get(address).status_code == 200


def test_the_address_of_an_archive_that_is_gone_says_not_found(client, tmp_path):
    address = zip_address(client, tmp_path)
    for f in sorted((tmp_path / "dl").rglob("*"), reverse=True):
        f.unlink() if f.is_file() else f.rmdir()

    assert client.get(address).status_code == 404


def restarted(tmp_path) -> TestClient:
    """The service started again on the same files."""
    return TestClient(create_app(tmp_path / "config.json"), headers=ORIGIN)


def test_an_address_still_works_after_a_restart(client, tmp_path):
    client.app.state.auth.download_links.clock = time.time  # the restarted service has the real clock
    address = zip_address(client, tmp_path)

    assert restarted(tmp_path).get(address).status_code == 200


def test_without_its_key_the_service_refuses_the_addresses_sent_so_far_and_signs_new_ones(client, tmp_path):
    client.app.state.auth.download_links.clock = time.time  # the restarted service has the real clock
    old = zip_address(client, tmp_path)
    (tmp_path / "data" / "download_links.json").unlink()
    assert client.get(old).status_code == 401  # at once, without a restart

    again = restarted(tmp_path)
    assert again.get(old).status_code == 401
    assert again.get(zip_address(again, tmp_path)).status_code == 200


def test_the_key_is_not_made_before_the_first_address_is_signed(client, tmp_path):
    assert not (tmp_path / "data" / "download_links.json").exists()
    assert client.get("/api/archives/zip", params={"folder": "A/x/y", "expires": "9999999999", "sig": "x"}).status_code == 401
    assert not (tmp_path / "data" / "download_links.json").exists()

    zip_address(client, tmp_path)
    assert "key" in (tmp_path / "data" / "download_links.json").read_text(encoding="utf-8")


# ---- the pipeline's messages ----

def pipeline_client(tmp_path, monkeypatch, auth: bool) -> tuple[TestClient, list]:
    """The service with automatic download for A; what its pipeline sends is collected."""
    cfg = write_config(tmp_path, auth=auth)
    body = json.loads(cfg.read_text(encoding="utf-8"))
    body["monitor"]["streamers"][0]["auto_download"] = True
    cfg.write_text(json.dumps(body), encoding="utf-8")
    if auth:
        assert cli(monkeypatch, cfg, "add-user", "boss", "--admin", "--password-stdin") == 0
    client = TestClient(create_app(cfg), headers=ORIGIN)
    sent: list = []
    ad = client.app.state.auto
    ad.notify, ad.site_url = sent.append, SITE
    return client, sent


def ready_address(client, sent) -> str:
    downloaded(client.app.state.auto)
    (ready,) = [e for e in sent if e.kind is EventKind.READY]
    return ready.links[0].url.removeprefix(SITE)


def test_ready_carries_an_address_that_downloads_without_logging_in(tmp_path, monkeypatch):
    client, sent = pipeline_client(tmp_path, monkeypatch, auth=True)
    address = ready_address(client, sent)

    assert set(parse_qs(urlsplit(address).query)) == {"folder", "expires", "sig"}
    assert client.get(address).status_code == 200


def test_without_login_on_the_dashboard_the_address_is_not_signed(tmp_path, monkeypatch):
    client, sent = pipeline_client(tmp_path, monkeypatch, auth=False)
    address = ready_address(client, sent)

    assert set(parse_qs(urlsplit(address).query)) == {"folder"}
    assert client.get(address).status_code == 200


def test_an_address_signed_by_another_process_on_the_same_files_works_in_the_running_service(client, tmp_path):
    client.app.state.auth.download_links.clock = time.time  # both processes have the real clock
    other = restarted(tmp_path)  # started before any key was made
    address = zip_address(other, tmp_path)

    assert client.get(address).status_code == 200
    assert client.get(zip_address(client, tmp_path, title="雑談")).status_code == 200  # and it signs with that key
    assert other.get(zip_address(client, tmp_path, title="雑談")).status_code == 200
