from contextlib import asynccontextmanager
from io import BytesIO

import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import WhisperModel
from typing import Annotated

MODEL_SIZE = "large-v3-turbo"

_model: WhisperModel | None = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(MODEL_SIZE, device="auto", compute_type="auto")
    return _model


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up the model on startup
    get_model()
    yield


app = FastAPI(title="Whisper ASR Server", lifespan=lifespan)


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = Form("ja"),
    beam_size: int = Form(5),
    vad_filter: bool = Form(False),
    vad_threshold: float = Form(0.5),
    min_silence_duration_ms: int = Form(500),
):
    """
    Transcribe an audio file using faster-whisper.

    Accepts raw PCM (16kHz mono int16) or any format FFmpeg/faster-whisper can decode.
    Returns the transcribed text.
    """
    raw = await audio.read()

    # If the upload is raw PCM int16 (what AudioMemoryCutter produces),
    # convert to float32 numpy array that faster-whisper expects.
    content_type = audio.content_type or ""
    if content_type == "audio/pcm" or audio.filename == "audio.pcm":
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        audio_input = samples
    else:
        # Let faster-whisper handle decoding (wav, mp3, etc.)
        audio_input = BytesIO(raw)

    model = get_model()
    segments, info = model.transcribe(
        audio_input,
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters=dict(
            threshold=vad_threshold,
            min_silence_duration_ms=min_silence_duration_ms,
        ) if vad_filter else None,
    )

    text = "".join(s.text for s in segments).strip()

    return {
        "text": text,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
    }


@app.post("/transcribe/batch")
async def transcribe_batch(
    audio: Annotated[list[UploadFile], File(...)],
    language: str = Form("ja"),
    beam_size: int = Form(5),
    vad_filter: bool = Form(False),
    vad_threshold: float = Form(0.5),
    min_silence_duration_ms: int = Form(500),
):
    """
    Transcribe multiple audio files in a single request.
    Returns a list of results in the same order as the uploaded files.
    """
    model = get_model()
    results = []

    for file in audio:
        raw = await file.read()

        content_type = file.content_type or ""
        if content_type == "audio/pcm" or (file.filename and file.filename.endswith(".pcm")):
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            audio_input = samples
        else:
            audio_input = BytesIO(raw)

        segments, info = model.transcribe(
            audio_input,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            vad_parameters=dict(
                threshold=vad_threshold,
                min_silence_duration_ms=min_silence_duration_ms,
            ) if vad_filter else None,
        )

        text = "".join(s.text for s in segments).strip()
        results.append({"text": text})

    return {"results": results}


@app.post("/transcribe/full")
async def transcribe_full(
    audio: UploadFile = File(...),
    language: str = Form("ja"),
    beam_size: int = Form(5),
    vad_filter: bool = Form(False),
    vad_threshold: float = Form(0.5),
    min_silence_duration_ms: int = Form(500),
):
    """
    Transcribe an audio file and return per-segment timestamps.

    Returns a list of segments with id, start, end, and text
    instead of concatenated text.
    """
    raw = await audio.read()

    content_type = audio.content_type or ""
    if content_type == "audio/pcm" or audio.filename == "audio.pcm":
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        audio_input = samples
    else:
        audio_input = BytesIO(raw)

    model = get_model()
    segments, info = model.transcribe(
        audio_input,
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters=dict(
            threshold=vad_threshold,
            min_silence_duration_ms=min_silence_duration_ms,
        ) if vad_filter else None,
    )

    result_segments = []
    for i, seg in enumerate(segments):
        result_segments.append({
            "id": i,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })

    return {
        "segments": result_segments,
        "language": info.language,
        "duration": info.duration,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_SIZE}
