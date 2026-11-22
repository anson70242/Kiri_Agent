"""The real drive in the pipeline: the service hands the automatic download the Quark drive
the config names (none without its CLI), says so once at start, and a processed live goes
through the Quark module, the fake CLI and back into 网盘上传完成, the archive and the queue view."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from backend import quark
from backend.archive import Archive
from backend.downloader.auto import load_auto
from backend.notify import EventKind

from .quark_fakes import PASSCODE, SHARE_URL
from .test_drive_stage import kinds, links, processed, step
from .test_quark_upload import cli, uploading  # noqa: F401  (pytest fixture)


def write_config(tmp_path, cli_file: Path | None) -> Path:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True}],
                    "streamers_file": str(tmp_path / "streamers.json")},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")}},
        "drive": {"quark_cli": str(cli_file or tmp_path / "nowhere.cjs")},
    }), encoding="utf-8")
    return cfg


def test_the_pipeline_gets_the_quark_drive_the_config_names_and_the_log_says_so_once(tmp_path, caplog):
    cli_file = tmp_path / "quark" / "quark-drive.cjs"
    cli_file.parent.mkdir()
    cli_file.write_text("// the CLI", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="backend.quark"):
        cfg = write_config(tmp_path, cli_file)
        ad = load_auto(cfg, quark.load_quark(cfg))

    assert isinstance(ad.drive, quark.Quark)
    assert ad.drive.cli == cli_file and ad.drive.downloads == tmp_path / "dl"
    assert [r.message for r in caplog.records if "drive" in r.message] == [f"drive available: Quark CLI at {cli_file}"]


def test_without_the_cli_the_pipeline_has_no_drive_and_the_log_says_so_once(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="backend.quark"):
        cfg = write_config(tmp_path, None)
        ad = load_auto(cfg, quark.load_quark(cfg))

    assert ad.drive is None
    assert len([r for r in caplog.records if "drive not available" in r.message]) == 1


def test_a_processed_live_goes_through_the_quark_module_into_its_message_the_archive_and_the_queue_view(tmp_path, cli):
    seen_views = []
    drive = quark.Quark(Path("/data/quark/scripts/quark-drive.cjs"), tmp_path / "dl")
    ad, _, sent, folder = processed(tmp_path, drive)
    uploading(cli)
    upload = cli.answers["upload"]

    def upload_watched(argv, cwd):
        lines = upload(argv, cwd)
        seen_views.append(ad.snapshot()["active"])
        return lines

    cli.answers["upload"] = upload_watched
    step(ad, "drive")

    address = f"{SHARE_URL}?pwd={PASSCODE}"
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY, EventKind.DRIVE_UPLOADED]
    assert links(sent[-1]) == [(address, "网盘链接")]
    assert Archive.from_folder(folder).read_drive_link().url == address
    assert seen_views and seen_views[0][0]["stage"] == "drive"
    assert not list((tmp_path / "dl").rglob("*.zip"))
