# Deployment

Ezra runs in two shapes from the same code. Configuration is environment variables (`EZRA_*`, see
`.env.example`), read from the process environment or a `.env` file in the working directory.

## 1. Local (one person, one machine)

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), ffmpeg + ffprobe (any recent build;
libass isn't needed because captions are drawn by Pillow), Node 22 for the dashboard.

```bash
uv sync --extra local                   # faster-whisper, OpenCV (headless 4.x), PySceneDetect
uv run ezra init                        # ./data: ezra.db (SQLite), storage/, cache/, work/
uv run ezra serve                       # API :8000 + an embedded worker
cd apps/web && npm install && npm run dev      # dashboard :3000 (EZRA_API_URL defaults to :8000)
```

- **Data** lives in `EZRA_HOME` (default `./data`). Back it up by copying the directory while
  nothing is running.
- **Models**: the first transcription downloads the Whisper model (`small` ≈ 480 MB) into
  `EZRA_MODEL_CACHE` (default `$EZRA_HOME/cache/models`); MediaPipe downloads its face model on
  first use.
- **Workers**: `ezra serve` runs one embedded worker. For parallel renders, add `ezra worker`
  processes; they coordinate through the database.
- **Apple Silicon / CPU**: `EZRA_WHISPER_COMPUTE=int8` (default) with batched decoding transcribes at
  about 5–6× real time with `small` on an M-series laptop (see BENCHMARK_REPORT.md).
  `EZRA_WHISPER_DEVICE=cuda` with `EZRA_WHISPER_COMPUTE=float16` on NVIDIA.

## 2. Docker Compose (team / server)

```bash
cp .env.example .env
# set at least:
#   EZRA_API_TOKEN=<random>      the dashboard proxy sends it; API calls need it
#   EZRA_SECRET_KEY=<random>     encrypts stored OAuth tokens, signs media links
#   POSTGRES_PASSWORD, MINIO_ROOT_USER, MINIO_ROOT_PASSWORD   (defaults are for local use only)
docker compose up -d --build
docker compose ps                      # postgres, minio, api (healthy), worker, web; migrate exited 0
open http://localhost:3000
```

| Service | Image | Role |
|---|---|---|
| `postgres` | postgres:17-alpine | Database (volume `postgres`) |
| `minio` | quay.io/minio/minio | S3-compatible storage (volume `minio`; console on 127.0.0.1:9001) |
| `migrate` | ezra (runtime) | `ezra db upgrade`, then exits; api and worker wait for it |
| `api` | ezra (runtime) | `ezra serve --no-embedded-worker` on :8000, healthcheck `/api/health` |
| `worker` | ezra (runtime) | `ezra worker`; scale with `docker compose up -d --scale worker=3` |
| `web` | ezra-web | Next.js standalone on :3000; proxies to `http://api:8000` with the token |
| `test` (profile) | ezra (test target) | `pytest` against Postgres + MinIO |
| `openshorts` (profile) | built from the OpenShorts repo | Optional second moment detector |

- The `ezra` image is `python:3.11-slim-bookworm` plus Debian ffmpeg and espeak-ng (fixtures), a
  uv-installed virtualenv with the `local` extra, and a non-root user. A build step asserts that
  OpenCV 4.x with Haar cascades is the importable `cv2`.
- Volumes: `ezra-home` (`/data`: work files, S3 download cache, the encrypted secret store) is
  shared by api and worker; `ezra-models` (`/models`) caches ML models across rebuilds.
- Ports: `EZRA_API_PORT` (8000) and `EZRA_WEB_PORT` (3000). If you change the API port or put it
  behind a domain, set `EZRA_PUBLIC_URL` (media links and OAuth redirect URIs are built from it)
  and `EZRA_WEB_URL`.
- CLI inside the stack: `docker compose run --rm api ezra campaign list`.
- Ingest server-side files: mount a folder into `api`/`worker` and list it in `EZRA_IMPORT_ROOTS`,
  or upload through the dashboard/API.
- Verify an installation end to end: `uv run python scripts/smoke_api.py --api http://localhost:8000
  --token $EZRA_API_TOKEN --video some.mp4` (creates a demo campaign, a source, clips and a
  local-export post).

### Production notes

- Put the API and dashboard behind TLS (any reverse proxy). Only the dashboard needs to be public
  if users go through it. The API must be reachable at `EZRA_PUBLIC_URL` by browsers for video
  playback (signed links) and by the OAuth providers for callbacks.
- Use managed Postgres / S3 by pointing `EZRA_DATABASE_URL` and `EZRA_S3_*` at them. Any
  S3-compatible store works (AWS S3, Cloudflare R2, MinIO); omit `EZRA_S3_ENDPOINT` for AWS.
- Rotate `EZRA_API_TOKEN` freely. `EZRA_SECRET_KEY` decrypts the secret store: rotating it means
  re-entering stored secrets (`ezra secrets set …`) and reconnecting OAuth accounts.
- Workers are CPU-heavy (Whisper, ffmpeg). Size them for about 1 CPU-minute per minute of source
  for analysis, plus roughly 1/3 of the clip length per render at 1080×1920 (measured on Apple
  M-series; see BENCHMARK_REPORT.md).
- GPU transcription: build a derived image with CUDA-enabled CTranslate2 and set
  `EZRA_WHISPER_DEVICE=cuda`, `EZRA_WHISPER_COMPUTE=float16`.

## Optional components

| Component | Enable | Notes |
|---|---|---|
| WhisperX (forced alignment) | `uv pip install whisperx`, `EZRA_TRANSCRIBER=whisperx` | Pulls PyTorch (~2 GB) |
| pyannote diarization | `uv pip install pyannote.audio`, `EZRA_DIARIZER=pyannote`, `HF_TOKEN` | Accept the `pyannote/speaker-diarization-community-1` terms on Hugging Face |
| YuNet faces (default `auto`) | nothing: the 227 KB model downloads to `EZRA_MODEL_CACHE` on first use | Falls back to Haar offline; `EZRA_FACE_DETECTOR=haar` to skip it |
| MediaPipe faces | `uv sync --extra local --extra mediapipe`, `EZRA_FACE_DETECTOR=mediapipe` | Downloads the BlazeFace model |
| Model critique | `EZRA_LLM=claude-cli` / `codex-cli` / `openai-compatible` (+ `EZRA_LLM_BASE_URL`, `EZRA_LLM_MODEL`) | CLIs use your existing subscription login; `openai-compatible` fits Ollama, LM Studio, vLLM |
| OpenShorts | `docker compose --profile openshorts up -d`, `EZRA_CLIP_ENGINE=openshorts`, `EZRA_OPENSHORTS_URL=http://localhost:8001` | Needs its own model (GEMINI_API_KEY or a local server) |
| Stock B-roll | `PEXELS_API_KEY` | Or `EZRA_BROLL_LIBRARY` for your own licensed clips |
| Publishing | see REMAINING_EXTERNAL_SETUP.md | OAuth apps per platform |
