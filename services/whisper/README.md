# whisper

faster-whisper (large-v3-turbo) behind a small FastAPI server, started by the project's
`docker-compose.yml` as the `whisper` service (GPU, no published port). kiri calls
`POST http://whisper:8000/transcribe/full` and gets per-segment timestamps back.

Copied from Yaki_Agent's `servers/whisper` so this project builds and runs it on its own.
The model is downloaded at image build time.

Endpoints: `/transcribe` (text), `/transcribe/batch` (several files), `/transcribe/full`
(segments with start/end), `/health`.
