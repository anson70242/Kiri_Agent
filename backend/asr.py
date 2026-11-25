"""Speech-to-text for a downloaded VOD through the `whisper` service (services/whisper).

    from backend.asr import ASRSettings, transcribe
    transcribe(Path("downloads/.../[20261110][A] title.mp4"), ASRSettings())
    # -> the transcript artifact next to the video (named by backend.archive)

The audio track is extracted with ffmpeg (16 kHz mono Opus, ~30 MB for 4 h) and sent to
`POST /transcribe/full` (Japanese, VAD on), two hours a request. Output, one segment per line:

    [
      {"start": 0.78, "end": 3.78, "text": "めっちゃ味するよ!"},
      ...
    ]

    uv run -m backend.asr <video>...              # transcribe by hand
    uv run -m backend.asr --singing <video>...    # a singing live: without voice detection
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from backend.archive import Archive, Artifact

log = logging.getLogger(__name__)


@dataclass
class ASRSettings:
    url: str = "http://whisper:8000"
    language: str = "ja"
    vad: bool = True
    timeout: float = 3 * 3600  # for one request
    ffmpeg: str = "ffmpeg"
    # Seconds of audio in one request. The whisper service holds a whole request in memory,
    # about 2 GB an hour: a live of 6 h 54 min sent whole took 14.7 GB and was killed by the
    # server, which has 15.5 GB (2026-11-17).
    piece: float = 2 * 3600


class ASRError(Exception):
    """Audio extraction or the whisper request failed."""


def transcribe(video: Path, settings: ASRSettings, client: httpx.Client | None = None,
               singing: bool = False) -> Path:
    """Transcribe `video` and save its transcript in its archive, next to it. The audio
    goes to the whisper service `settings.piece` seconds a request, one request after
    another; a line cut by the end of a piece comes back as two, and one that whisper
    places after the end of its piece is dropped. `singing`: a singing
    live, transcribed without voice detection (see `request_segments`)."""
    segments: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        pieces = extract_audio(video, Path(tmp), settings.ffmpeg, settings.piece)
        for i, audio in enumerate(pieces):
            log.info("transcribing %s: piece %d/%d", video.name, i + 1, len(pieces))
            starts = i * settings.piece  # where this piece starts in the live
            segments += [
                {**s, "start": round(s["start"] + starts, 2), "end": round(s["end"] + starts, 2)}
                for s in request_segments(audio, settings, client, singing)
                if s["start"] < settings.piece  # whisper can time a piece's last line after its end
            ]
    out = Archive.from_video(video).save(Artifact.TRANSCRIPT, segments)
    log.info("transcribed %s: %d segments", video.name, len(segments))
    return out


def extract_audio(video: Path, out_dir: Path, ffmpeg: str = "ffmpeg", piece: float = ASRSettings.piece) -> list[Path]:
    """The audio of `video` as files of `piece` seconds each (the last one shorter), in order."""
    cmd = [ffmpeg, "-loglevel", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "libopus", "-b:a", "24k",
           "-f", "segment", "-segment_time", str(piece), "-reset_timestamps", "1", str(out_dir / "audio%03d.ogg")]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ASRError(f"ffmpeg not found ({ffmpeg})") from e
    except subprocess.CalledProcessError as e:
        raise ASRError(f"ffmpeg failed: {(e.stderr or '').strip()[:300]}") from e
    pieces = sorted(out_dir.glob("audio*.ogg"))
    if not pieces:
        raise ASRError("ffmpeg wrote no audio")
    return pieces


def request_segments(audio: Path, settings: ASRSettings, client: httpx.Client | None = None,
                     singing: bool = False) -> list[dict[str, Any]]:
    """The whisper service's segments for `audio`, timed from the start of `audio`.

    `singing`: without voice detection, which drops quiet singing over music (on a "sleep"
    singing live of 2 h 13 min, 2026-11-16, song naming found 3 songs with it and 8
    without), and without conditioning each window on the previous text: with nothing
    filtered out, one invented line over the waiting-room music otherwise repeats for 25
    minutes. What whisper still invents over music alone is dropped (`invented`)."""
    own = client is None
    client = client or httpx.Client(base_url=settings.url, timeout=settings.timeout)
    data = {"language": settings.language, "vad_filter": str(settings.vad).lower()}
    if singing:
        data |= {"vad_filter": "false", "condition_on_previous_text": "false"}
    try:
        with audio.open("rb") as f:
            resp = client.post("/transcribe/full", files={"audio": (audio.name, f, "audio/ogg")}, data=data)
        resp.raise_for_status()
        raw = resp.json().get("segments") or []
    except (httpx.HTTPError, ValueError) as e:
        raise ASRError(f"whisper request failed: {e}") from e
    finally:
        if own:
            client.close()
    segments = [
        {"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": text}
        for s in raw
        if (text := str(s.get("text") or "").strip())
    ]
    return [s for s in segments if not invented(s)] if singing else segments


# Without voice detection, a 30-second window holding no speech comes back as one short
# stock line stretched over the whole window ("ご視聴ありがとうございました", 14 characters in
# 30 s). Speech and singing are denser: the slowest sung lines measured were 1.1 characters
# a second, and a held note is shorter than this.
INVENTED_FROM = 20.0  # seconds
INVENTED_BELOW = 1.0  # characters a second


def invented(segment: dict[str, Any]) -> bool:
    """Whether a segment is a line whisper made up for a window without speech."""
    length = segment["end"] - segment["start"]
    return length >= INVENTED_FROM and len(segment["text"]) < INVENTED_BELOW * length


def main() -> int:
    p = argparse.ArgumentParser(description="Transcribe videos through the whisper service.")
    p.add_argument("videos", nargs="+", type=Path)
    p.add_argument("--url", default=ASRSettings.url)
    p.add_argument("--singing", action="store_true", help="singing lives: transcribe whole, keeping quiet singing")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = ASRSettings(url=args.url)
    for video in args.videos:
        try:
            print(transcribe(video, settings, singing=args.singing))
        except ASRError as e:
            print(f"{video.name}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
