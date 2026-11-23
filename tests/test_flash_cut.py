"""Cutting a video too big for one file of a flash transfer: the plan and the parts' names,
tested with numbers."""

import pytest

from backend.flash.cut import CutError, cut, part_name, plan

GB = 1_000_000_000
HOUR = 3600.0


def test_a_video_at_or_below_the_limit_is_not_cut():
    assert plan(7 * GB, 3 * HOUR) == [(0.0, 3 * HOUR)]
    assert plan(10 * GB, 4 * HOUR) == [(0.0, 4 * HOUR)]


def test_a_video_over_the_limit_is_cut_into_the_fewest_equal_parts_under_the_cutting_size():
    assert plan(10 * GB + 1, 4 * HOUR) == [(0.0, 2 * HOUR), (2 * HOUR, 4 * HOUR)]
    assert plan(25 * GB, 9 * HOUR) == [(0.0, 3 * HOUR), (3 * HOUR, 6 * HOUR), (6 * HOUR, 9 * HOUR)]


def test_a_part_expected_at_exactly_the_cutting_size_is_one_part_too_few():
    # 19 GB in two parts is 9.5 GB each: not under the cutting size, so three parts
    assert len(plan(19 * GB, 6 * HOUR)) == 3


def test_the_limit_and_the_cutting_size_can_be_lowered():
    assert plan(1000, 60.0, limit=500, cutting_size=400) == [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0)]
    assert plan(500, 60.0, limit=500, cutting_size=400) == [(0.0, 60.0)]


def test_a_part_is_named_with_its_number_the_count_and_its_place_in_the_live_in_dotted_times():
    name = part_name("[20261122][森野鈴] 标题", ".mp4", 2, 3, start=11565.7, end=22810.2)
    assert name == "[20261122][森野鈴] 标题 (2／3 03.12.45–06.20.10).mp4"


def test_a_part_name_counts_hours_past_a_day_and_holds_no_colon():
    name = part_name("[20261122][青葉みのり] 耐久", ".mp4", 3, 3, start=86399.0, end=100000.0)
    assert name == "[20261122][青葉みのり] 耐久 (3／3 23.59.59–27.46.40).mp4"
    assert ":" not in name


def test_a_video_that_fits_is_answered_as_it_is_without_running_ffmpeg(tmp_path):
    video = tmp_path / "[20261122][森野鈴] 标题.mp4"
    video.write_bytes(b"x" * 500)
    folder = tmp_path / "staging"

    files = cut(video, folder, limit=500, cutting_size=400, ffmpeg="no-such-ffmpeg", ffprobe="no-such-ffprobe")

    assert files == [video]
    assert video.read_bytes() == b"x" * 500
    assert not folder.exists() or not any(folder.iterdir())


def test_a_video_over_the_limit_without_ffmpeg_raises_and_writes_nothing(tmp_path):
    video = tmp_path / "[20261122][森野鈴] 标题.mp4"
    video.write_bytes(b"x" * 1000)
    folder = tmp_path / "staging"

    with pytest.raises(CutError):
        cut(video, folder, limit=500, cutting_size=400, ffmpeg="no-such-ffmpeg", ffprobe="no-such-ffprobe")

    assert not folder.exists() or not any(folder.iterdir())
