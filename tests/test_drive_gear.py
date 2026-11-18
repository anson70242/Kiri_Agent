"""The gear's "自动上传到夸克网盘": the drive checkbox and drive folder, through
/api/streamers and the 直播状态 page. Nothing is uploaded here: the gear ends at the
map "streamer id -> drive folder" handed to the running pipeline.
"""

from backend.archive import name_archive

from .test_monitor_page import browser, expect_text, page, roster, server  # noqa: F401 (fixtures)
from .test_streamers_api import app_for, write_config


def streamer(client, sid):
    return next(s for s in client.get("/api/streamers").json()["streamers"] if s["id"] == sid)


def test_ticking_the_drive_box_needs_no_folder(tmp_path):
    cfg = write_config(tmp_path)
    client = app_for(cfg)

    r = client.patch("/api/streamers", params={"id": "youtube:@a"}, json={"drive": True})

    assert r.status_code == 200
    a = streamer(app_for(cfg), "youtube:@a")  # kept across a restart
    assert (a["drive"], a["drive_folder"], a["drive_folder_default"]) == (True, "", "录播/A")
    assert client.app.state.auto.drive_folders == {"youtube:@a": "录播/A"}


def test_the_default_folder_names_the_streamer_as_their_archives_are_filed(tmp_path):
    name = "AC/DC: 本人?"  # a slash and characters a folder cannot have
    cfg = write_config(tmp_path, streamers=[{"name": name, "platform": "youtube", "channel": "@ac"}])
    client = app_for(cfg)

    assert client.patch("/api/streamers", params={"id": "youtube:@ac"}, json={"drive": True}).status_code == 200

    expected = "录播/AC_DC_ 本人_"  # one folder, named like the archives' creator folder
    assert expected == f"录播/{name_archive(name, '20261105', '歌枠')[0].parts[0]}"
    assert streamer(client, "youtube:@ac")["drive_folder_default"] == expected
    assert client.app.state.auto.drive_folders == {"youtube:@ac": expected}


def test_the_folder_is_trimmed_and_its_slashes_stored_as_forward_ones(tmp_path):
    cfg = write_config(tmp_path)
    client = app_for(cfg)

    r = client.patch("/api/streamers", params={"id": "youtube:@a"},
                     json={"drive": True, "drive_folder": "  /直播录像\\某主播/ "})

    assert r.status_code == 200
    assert streamer(app_for(cfg), "youtube:@a")["drive_folder"] == "直播录像/某主播"
    assert client.app.state.auto.drive_folders == {"youtube:@a": "直播录像/某主播"}


def test_a_folder_a_drive_cannot_have_is_refused(tmp_path):
    cfg = write_config(tmp_path)
    client = app_for(cfg)
    patch = lambda folder: client.patch("/api/streamers", params={"id": "youtube:@a"},  # noqa: E731
                                        json={"drive": True, "drive_folder": folder})

    for folder in ["录播//A", "录播/./A", "../A", "录播/A:B", "A*", "A?", 'A"', "A<", "A>", "A|"]:
        r = patch(folder)
        assert r.status_code == 422, folder
        assert "网盘文件夹" in r.json()["detail"], folder

    a = streamer(app_for(cfg), "youtube:@a")  # nothing saved
    assert (a["drive"], a["drive_folder"]) == (False, "")
    assert client.app.state.auto.drive_folders == {}


def test_the_drive_box_is_independent_of_tencent_docs(tmp_path):
    client = app_for(write_config(tmp_path))
    patch = lambda **body: client.patch("/api/streamers", params={"id": "youtube:@a"}, json=body)  # noqa: E731
    auto = client.app.state.auto

    assert patch(upload=True, upload_sheet="DUmFabcXYZ123").status_code == 200
    assert (auto.uploads, auto.drive_folders) == ({"youtube:@a": "DUmFabcXYZ123"}, {})

    assert patch(drive=True, drive_folder="直播录像").status_code == 200
    assert (auto.uploads, auto.drive_folders) == ({"youtube:@a": "DUmFabcXYZ123"}, {"youtube:@a": "直播录像"})

    assert patch(upload=False).status_code == 200
    assert (auto.uploads, auto.drive_folders) == ({}, {"youtube:@a": "直播录像"})

    assert patch(drive=False).status_code == 200  # unticked: the folder is kept for next time
    assert auto.drive_folders == {}
    assert streamer(client, "youtube:@a")["drive_folder"] == "直播录像"


def test_at_start_the_pipeline_gets_the_ticked_streamers_folders(tmp_path):
    cfg = write_config(tmp_path, streamers=[
        {"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True, "drive": True},
        {"name": "B", "platform": "twitch", "channel": "b", "drive": True, "drive_folder": "直播录像/B"},
        {"name": "C", "platform": "twitch", "channel": "c", "drive_folder": "直播录像/C"},  # not ticked
        {"name": "D", "platform": "twitch", "channel": "d"},  # an entry from before the drive: off and blank
    ])
    client = app_for(cfg)

    assert client.app.state.auto.drive_folders == {"youtube:@a": "录播/A", "twitch:b": "直播录像/B"}
    d = streamer(client, "twitch:d")
    assert (d["drive"], d["drive_folder"], d["drive_folder_default"]) == (False, "", "录播/D")


# ---- the gear on the 直播状态 page ----

def test_gear_ticks_the_drive_box_and_the_card_shows_it(page, server):
    url, cfg = server
    page.goto(url)
    page.get_by_role("button", name="设置 A").click()
    dlg = page.get_by_role("dialog")
    folder = dlg.get_by_label("网盘文件夹")
    assert folder.get_attribute("placeholder") == "录播/A"  # blank means the default
    dlg.get_by_label("自动上传到夸克网盘").check()
    folder.fill("录播/A:B")
    dlg.get_by_role("button", name="保存").click()
    expect_text(dlg, "网盘文件夹")  # stays open with the reason

    folder.fill("直播录像/A")
    dlg.get_by_role("button", name="保存").click()
    dlg.wait_for(state="hidden")

    a = roster(cfg)["youtube:@a"]
    assert (a["drive"], a["drive_folder"], a["upload"]) == (True, "直播录像/A", False)
    expect_text(page.locator("body"), "☁ 网盘")
