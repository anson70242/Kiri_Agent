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


def form_fields(request: httpx.Request) -> dict[str, str]:
    """The plain form fields of a multipart request (not the audio)."""
    fields = {}
    for part in request.read().decode("latin-1").split("\r\n--")[:-1]:
        head, _, value = part.partition("\r\n\r\n")
        if 'name="' in head and "filename=" not in head:
            fields[head.split('name="')[1].split('"')[0]] = value
    return fields


@needs_ffmpeg
def test_a_singing_live_is_transcribed_whole_so_quiet_singing_keeps_its_lyrics(tmp_path):
    video = make_video(tmp_path / "[20261110][A] 歌枠.mp4")
    whisper = FakeWhisper([{"id": 0, "start": 1.0, "end": 4.0, "text": "あめあめ ふらふら"}])

    transcribe(video, ASRSettings(), client=whisper.client(), singing=True)

    fields = form_fields(whisper.requests[0])
    assert fields["vad_filter"] == "false"  # voice detection drops quiet singing
    assert fields["condition_on_previous_text"] == "false"  # or one hallucinated line repeats for minutes
    assert fields["language"] == "ja"


@needs_ffmpeg
def test_a_singing_live_loses_the_lines_whisper_invents_over_music(tmp_path):
    video = make_video(tmp_path / "[20261110][A] 歌枠.mp4")
    invented = "ご視聴ありがとうございました"  # what whisper writes for a 30-second window with no speech
    whisper = FakeWhisper([
        {"id": 0, "start": 0.0, "end": 29.98, "text": invented},  # the waiting room: a whole window, one short line
        {"id": 1, "start": 30.0, "end": 59.98, "text": invented},
        {"id": 2, "start": 120.0, "end": 122.5, "text": "さて、はじめます"},
        {"id": 3, "start": 200.0, "end": 213.0, "text": "ゆっくり ながれる ほしのうた"},  # a slow sung line: kept
        {"id": 4, "start": 230.0, "end": 239.0, "text": "あるきだす"},  # a held note: short, kept
        {"id": 5, "start": 7000.0, "end": 7002.5, "text": invented},  # the streamer really says it: kept
    ])

    out = transcribe(video, ASRSettings(), client=whisper.client(), singing=True)

    assert [line["text"] for line in json.loads(out.read_text(encoding="utf-8"))] == [
        "さて、はじめます", "ゆっくり ながれる ほしのうた", "あるきだす", invented]


@needs_ffmpeg
def test_any_other_live_is_asked_as_before(tmp_path):
    video = make_video(tmp_path / "[20261110][A] 杂谈.mp4")
    whisper = FakeWhisper([{"id": 0, "start": 1.0, "end": 4.0, "text": "こんばんは"}])

    transcribe(video, ASRSettings(), client=whisper.client())

    assert form_fields(whisper.requests[0]) == {"language": "ja", "vad_filter": "true"}


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
