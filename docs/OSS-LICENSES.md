# Open-source components and licenses

What Ezra uses, how, and under which license. Versions are the ones locked in `uv.lock` /
`apps/web/package-lock.json` at the time of writing. License expressions come from each package's
own metadata; project-level facts were checked against the upstream repositories and model cards.

## Python dependencies (installed)

| Package | Version | License | Use |
|---|---|---|---|
| FastAPI / Starlette | 0.141.1 / 1.6.0 | MIT / BSD-3-Clause | REST API |
| uvicorn | 0.53.0 | BSD-3-Clause | ASGI server |
| SQLAlchemy / Alembic | 2.0.54 / 1.20.0 | MIT / MIT | ORM, migrations |
| psycopg (binary) | 3.3.6 | LGPL-3.0-only | PostgreSQL driver, used unmodified as a library |
| boto3 | 1.43.100 | Apache-2.0 | S3 storage |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | Fernet secret store |
| pydantic / pydantic-settings | 2.13.5 / 2.15.0 | MIT | Schemas, configuration |
| typer / rich | 0.27.2 / 15.0.0 | MIT | CLI |
| mcp (Python SDK) | 2.2.0 | MIT | MCP server |
| httpx | 0.28.1 | BSD-3-Clause | Platform API clients |
| numpy / scipy | 2.4.6 / 1.17.1 | BSD-3-Clause (+ bundled permissive) / BSD-3-Clause | Signal processing, clustering, statistics |
| Pillow | 12.3.0 | MIT-CMU | Caption, card and overlay drawing |
| PyYAML, python-multipart | 6.0.3, 0.0.32 | MIT, Apache-2.0 | Campaign files, uploads |
| faster-whisper / CTranslate2 | 1.2.1 / 4.8.2 | MIT / MIT | Transcription (`local` extra) |
| PyAV | 18.1.0 | BSD-3-Clause (bundles LGPL FFmpeg libraries) | Audio decoding inside faster-whisper |
| onnxruntime, tokenizers, huggingface-hub | 1.30.0, 0.23.2, 1.32.0 | MIT, Apache-2.0, Apache-2.0 | faster-whisper runtime + model download |
| opencv-python-headless | 4.14.0.94 | Apache-2.0 (bundles LGPL FFmpeg) | Face detection (Haar cascades), frame sampling |
| PySceneDetect (`scenedetect`) | 0.7.1 | BSD-3-Clause | Scene-cut detection |

Development only: pytest (MIT), ruff (MIT), mypy (MIT), moto (Apache-2.0), scikit-image (BSD-3-Clause),
boto3-stubs (MIT), types-PyYAML (Apache-2.0).

## Optional, installed on demand (not in the default lock)

| Component | License | Use |
|---|---|---|
| WhisperX | BSD-2-Clause | `EZRA_TRANSCRIBER=whisperx` (forced alignment); pulls PyTorch (BSD-3-Clause) |
| pyannote.audio | MIT | `EZRA_DIARIZER=pyannote` |
| pyannote/speaker-diarization-community-1 (model) | CC-BY-4.0, **gated** (accept conditions on Hugging Face) | Diarization model weights; attribution required if redistributed; Ezra only downloads it with your `HF_TOKEN` |
| MediaPipe (`mediapipe` extra) + BlazeFace short-range model | Apache-2.0 | `EZRA_FACE_DETECTOR=mediapipe` |
| yt-dlp | Unlicense | URL ingest and live YouTube input, only when you ask for it |
| OpenShorts (github.com/mutonby/openshorts) | MIT for the core app; its `cloud/` directory is under the "OpenShorts Commercial License" (source-available) | Optional second moment detector, talked to over its REST API. Ezra neither copies nor uses anything from `cloud/`; the compose profile builds the upstream repository unchanged. |

## Models downloaded at runtime

| Model | License | Notes |
|---|---|---|
| Whisper weights via `Systran/faster-whisper-*` | MIT (OpenAI Whisper) | Downloaded into `EZRA_MODEL_CACHE` on first use |
| OpenCV Haar cascades | Apache-2.0 (shipped in the OpenCV 4.x wheel) | Why OpenCV is pinned below 5 |
| YuNet face detector (`face_detection_yunet_2023mar.onnx`, opencv_zoo) | MIT | Default face detector; 227 KB, fetched once into `EZRA_MODEL_CACHE` (size checked) |
| PP-OCRv3 text detector (`text_detection_en_ppocrv3_2023may.onnx`, opencv_zoo, from PaddleOCR) | Apache-2.0 | Default text detector for graphics protection; 2.4 MB, fetched once into `EZRA_MODEL_CACHE` (size checked) |
| Apple Color Emoji / Noto Color Emoji | System font / SIL OFL 1.1 | Emoji captions; used from the OS (Noto installed in the Docker image) |

## External programs (invoked as separate processes)

| Program | License | Notes |
|---|---|---|
| FFmpeg / ffprobe | LGPL-2.1+ or GPL-2+, depending on the build | Ezra calls the binary; it doesn't link it. Debian's build in the Docker image and Homebrew's local build include GPL components (libx264), so the binary is GPL. If you redistribute the Docker image, also offer the corresponding Debian source packages. |
| x264 (inside FFmpeg) | GPL-2+ | H.264 encoding |
| espeak-ng | GPL-3.0+ | Only synthesizes benchmark/test fixtures (Docker test image, Linux) |
| macOS `say` | Apple system software | Only synthesizes fixtures locally. Generated audio isn't committed, since Apple's voices are licensed for personal use. |
| Claude Code CLI / Codex CLI | Their vendors' terms | Optional `EZRA_LLM` providers, using the user's own login |

## Services in docker-compose

| Service | License | Notes |
|---|---|---|
| PostgreSQL 17 | PostgreSQL License | Unmodified official image |
| MinIO (quay.io/minio/minio) | AGPL-3.0 | Unmodified, run as a separate network service. Ezra talks to it through the S3 API with boto3 and doesn't link or modify it; swap in any S3-compatible store (AWS S3, R2, SeaweedFS, Garage) with `EZRA_S3_*`. |
| Node.js 22 image | MIT (Node) | Dashboard runtime |

## Dashboard (apps/web)

| Package | Version | License |
|---|---|---|
| next | 16.3.6 | MIT |
| react / react-dom | 19.3.0 | MIT |
| typescript | 5.9.3 | Apache-2.0 |
| eslint / eslint-config-next | 9.39.5 / 16.3.6 | MIT |

## Assets

- `src/ezra/benchmark/assets/portrait.jpg`: NASA astronaut portrait, a U.S. government work in the
  public domain. Provenance is in `src/ezra/benchmark/assets/README.md`.
- Caption fonts: system fonts found at runtime (e.g. DejaVu Sans Bold, Bitstream Vera license) or
  your own `EZRA_FONT_PATH`. No fonts are bundled.

## Referenced for concepts only (no code used)

| Project | License | What informed Ezra |
|---|---|---|
| ClipsAI | MIT | Transcript-driven clip boundaries, resizing around speakers |
| OpusClip, quso.ai, reap | Proprietary | Product ideas (virality-style scoring, caption styles, review UX); no code, assets or APIs |
| Remotion | Remotion company license (paid for many companies) | Deliberately **not** used; rendering is ffmpeg + Pillow |
