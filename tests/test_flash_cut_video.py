"""Cutting a real video, made by ffmpeg in the test, with the size limit lowered through the
arguments (skipped when ffmpeg is absent)."""

import re
import shutil
import subprocess

import pytest

from backend.flash.cut import CutError, cut

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg not installed")

STEM = "[20261122][森野鈴] 标题"
PART = re.compile(re.escape(STEM) + r" \((\d)／(\d) (\d\d\.\d\d\.\d\d)–(\d\d\.\d\d\.\d\d)\)\.mp4")


def make_video(path, source="testsrc=size=64x64:rate=10:duration=6", sound=True, key_frames_every=5):
    """A 6 s video, by default with a key frame every half second, so that it can be cut finely."""
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "aac"] if sound else []
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, *audio,
         "-c:v", "libx264", "-g", str(key_frames_every), "-shortest", str(path)],
        check=True,
    )
    return path


# 3 s of a still picture, then 3 s of noise: nearly all of the bytes are in the second half
UNEVEN = ("color=size=160x120:rate=10:duration=3[still];"
          "testsrc=size=160x120:rate=10:duration=3,noise=alls=100:allf=t[noisy];"
          "[still][noisy]concat=n=2[out0]")


def probe(path, entries):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", f"format={entries}", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return [float(v) for v in out.strip().split(",")]


def first_frame_is_a_key_frame(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-read_intervals", "%+#1",
                          "-show_entries", "frame=key_frame", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return out.splitlines()[0].startswith("1")


def test_a_video_over_the_limit_is_cut_into_parts_that_each_play_from_their_start(tmp_path):
    video = make_video(tmp_path / f"{STEM}.mp4")
    size = video.stat().st_size
    folder = tmp_path / "staging"

    parts = cut(video, folder, limit=size // 2, cutting_size=size * 2 // 5)

    assert [p.parent for p in parts] == [folder] * 3
    assert sorted(folder.iterdir()) == sorted(parts)
    names = [PART.fullmatch(p.name) for p in parts]
    assert all(names), [p.name for p in parts]
    assert [(m[1], m[2]) for m in names] == [("1", "3"), ("2", "3"), ("3", "3")]
    assert names[0][3] == "00.00.00"
    assert [m[4] for m in names[:-1]] == [m[3] for m in names[1:]]  # each part begins where the last ended
    for part in parts:
        assert part.stat().st_size <= size // 2
        assert first_frame_is_a_key_frame(part)
        assert probe(part, "start_time")[0] < 0.5
    assert sum(probe(p, "duration")[0] for p in parts) == pytest.approx(probe(video, "duration")[0], abs=0.3)
    assert video.stat().st_size == size  # the video itself is left as it was


def test_a_part_over_the_limit_makes_the_plan_again_with_one_part_more(tmp_path):
    video = make_video(tmp_path / f"{STEM}.mp4", UNEVEN, sound=False)
    size = video.stat().st_size
    folder = tmp_path / "staging"

    # planned as 2 parts of 3 s; the second holds all the noise and comes out over the limit
    parts = cut(video, folder, limit=size * 3 // 4, cutting_size=size * 3 // 5)

    assert [PART.fullmatch(p.name)[2] for p in parts] == ["3"] * 3
    assert sorted(folder.iterdir()) == sorted(parts)  # nothing of the first attempt is left
    assert all(p.stat().st_size <= size * 3 // 4 for p in parts)


def test_cutting_that_fails_raises_and_leaves_no_parts_behind(tmp_path):
    video = make_video(tmp_path / f"{STEM}.mp4")
    size = video.stat().st_size
    folder = tmp_path / "staging"

    with pytest.raises(CutError, match="no-such-ffmpeg"):
        cut(video, folder, limit=size // 2, cutting_size=size * 2 // 5, ffmpeg="no-such-ffmpeg")

    assert list(folder.iterdir()) == []


def test_a_video_no_cut_can_bring_under_the_limit_raises_and_leaves_no_parts_behind(tmp_path):
    video = make_video(tmp_path / f"{STEM}.mp4", key_frames_every=1000)  # one key frame: no place to cut
    size = video.stat().st_size
    folder = tmp_path / "staging"

    with pytest.raises(CutError, match="over the limit"):
        cut(video, folder, limit=size // 2, cutting_size=size * 2 // 5)

    assert list(folder.iterdir()) == []
