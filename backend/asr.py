"""Speech-to-text for a downloaded VOD through the `whisper` service (services/whisper).

    from backend.asr import ASRSettings, transcribe
    transcribe(Path("downloads/.../[20261110][A] title.mp4"), ASRSettings())
    # -> "[20261110][A] title.transcript.json" next to the video

The audio track is extracted with ffmpeg (16 kHz mono Opus, ~30 MB for 4 h) and sent
whole to `POST /transcribe/full` (Japanese, VAD on). Output, one segment per line:

    [
      {"start": 0.78, "end": 3.78, "text": "めっちゃ味するよ!"},
      ...
    ]

    uv run -m backend.asr <video>...     # transcribe by hand
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)
SUFFIX = ".transcript.json"


@dataclass
class ASRSettings:
    url: str = "http://whisper:8000"
    language: str = "ja"
    vad: bool = True
    timeout: float = 3 * 3600  # one request covers the whole stream
    ffmpeg: str = "ffmpeg"


class ASRError(Exception):
    """Audio extraction or the whisper request failed."""


def transcript_path(video: Path) -> Path:
    return video.with_name(video.name.removesuffix(video.suffix) + SUFFIX)


def transcribe(video: Path, settings: ASRSettings, client: httpx.Client | None = None) -> Path:
    """Transcribe `video` and write `<stem>.transcript.json` next to it."""
    out = transcript_path(video)
    with tempfile.TemporaryDirectory() as tmp:
        audio = extract_audio(video, Path(tmp) / "audio.ogg", settings.ffmpeg)
        segments = request_segments(audio, settings, client)
    write_segments(segments, out)
    log.info("transcribed %s: %d segments", video.name, len(segments))
    return out


def extract_audio(video: Path, out: Path, ffmpeg: str = "ffmpeg") -> Path:
    cmd = [ffmpeg, "-loglevel", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "libopus", "-b:a", "24k", str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ASRError(f"ffmpeg not found ({ffmpeg})") from e
    except subprocess.CalledProcessError as e:
        raise ASRError(f"ffmpeg failed: {(e.stderr or '').strip()[:300]}") from e
    return out


def request_segments(audio: Path, settings: ASRSettings, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    own = client is None
    client = client or httpx.Client(base_url=settings.url, timeout=settings.timeout)
    try:
        with audio.open("rb") as f:
            resp = client.post(
                "/transcribe/full",
                files={"audio": (audio.name, f, "audio/ogg")},
                data={"language": settings.language, "vad_filter": str(settings.vad).lower()},
            )
        resp.raise_for_status()
        raw = resp.json().get("segments") or []
    except (httpx.HTTPError, ValueError) as e:
        raise ASRError(f"whisper request failed: {e}") from e
    finally:
        if own:
            client.close()
    return [
        {"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": text}
        for s in raw
        if (text := str(s.get("text") or "").strip())
    ]


def write_segments(segments: list[dict[str, Any]], out: Path) -> None:
    body = ",\n".join("  " + json.dumps(s, ensure_ascii=False) for s in segments)
    tmp = out.with_name(out.name + ".part")
    tmp.write_text(f"[\n{body}\n]\n" if segments else "[]\n", encoding="utf-8")
    tmp.replace(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Transcribe videos through the whisper service.")
    p.add_argument("videos", nargs="+", type=Path)
    p.add_argument("--url", default=ASRSettings.url)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = ASRSettings(url=args.url)
    for video in args.videos:
        try:
            print(transcribe(video, settings))
        except ASRError as e:
            print(f"{video.name}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
