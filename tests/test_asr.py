"""Speech-to-text through the whisper service: audio extraction, request, transcript file."""

import json
import shutil
import subprocess

import httpx
import pytest

from backend.archive import Archive, Artifact
from backend.asr import ASRError, ASRSettings, transcribe

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_video(path, seconds=2):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-f", "lavfi", "-i", f"color=size=64x64:duration={seconds}", "-shortest", str(path)],
        check=True,
    )
    return path


class FakeWhisper:
    """Stands in for services/whisper: records the request, answers /transcribe/full."""

    def __init__(self, segments):
        self.segments = segments
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/transcribe/full"
        return httpx.Response(200, json={"segments": self.segments, "language": "ja", "duration": 2.0})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self), base_url="http://whisper:8000")


@needs_ffmpeg
def test_transcribe_writes_transcript_next_to_video(tmp_path):
    video = make_video(tmp_path / "[20261110][A] title.mp4")
    whisper = FakeWhisper([
        {"id": 0, "start": 0.78, "end": 3.78, "text": "めっちゃ味するよ!"},
        {"id": 1, "start": 3.78, "end": 6.38, "text": "  コメントは  "},
        {"id": 2, "start": 7.0, "end": 8.0, "text": ""},  # empty: dropped
    ])

    out = transcribe(video, ASRSettings(), client=whisper.client())

    assert out == tmp_path / "[20261110][A] title.transcript.json"
    assert json.loads(out.read_text(encoding="utf-8")) == [
        {"start": 0.78, "end": 3.78, "text": "めっちゃ味するよ!"},
        {"start": 3.78, "end": 6.38, "text": "コメントは"},
    ]
    (req,) = whisper.requests
    body = req.read().decode("latin-1")
    assert 'name="language"\r\n\r\nja' in body
    assert 'name="vad_filter"\r\n\r\ntrue' in body
    assert 'name="audio"' in body  # the extracted audio, not the whole video


@needs_ffmpeg
def test_whisper_error_raises_and_writes_nothing(tmp_path):
    video = make_video(tmp_path / "v.mp4")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="CUDA OOM")),
                          base_url="http://whisper:8000")
    with pytest.raises(ASRError, match="500"):
        transcribe(video, ASRSettings(), client=client)
    assert not Archive.from_video(video).has(Artifact.TRANSCRIPT)


def test_unreadable_video_raises(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(ASRError, match="ffmpeg"):
        transcribe(bad, ASRSettings(), client=FakeWhisper([]).client())
