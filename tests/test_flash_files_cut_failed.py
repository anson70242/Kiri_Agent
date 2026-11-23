"""A cutting that fails: the reason the files could not be prepared is short and Chinese (the
owner reads it in the alert of a flash transfer given up on), and ffmpeg's own words go to the
log. Through the files of a flash transfer, with an ffmpeg that is not there."""

from __future__ import annotations

import logging
import re

import pytest

from backend.archive import Archive
from backend.flash.client import FlashFailed
from backend.flash.files import prepare

from .helpers import sample_archive


def test_a_failed_cutting_is_told_in_short_chinese_and_logged_in_ffmpegs_words(tmp_path, caplog):
    root = tmp_path / "downloads"
    folder = sample_archive(root, video=b"x" * 100)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(FlashFailed) as failed:
        prepare(Archive.from_folder(folder), root, ffmpeg=str(tmp_path / "no-ffmpeg-here" / "ffmpeg"), limit=10)

    reason = str(failed.value)
    assert "切" in reason and re.search("[A-Za-z]", reason) is None and len(reason) <= 30
    assert "not found" in caplog.text
