# Ezra API / worker / CLI image.
#   docker compose up            (see docker-compose.yml)
#   docker build --target test . (image with the dev tools and the test suite)
#
# Debian's ffmpeg is built with libass, libfreetype and loudnorm; espeak-ng is
# only needed to synthesise the benchmark and test fixtures.
FROM python:3.11-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core fonts-noto-color-emoji \
    libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app

# dependencies first so source edits don't reinstall them
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra local --no-install-project
COPY src ./src
COPY campaigns ./campaigns
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra local

ENV PATH=/app/.venv/bin:$PATH EZRA_HOME=/data EZRA_MODEL_CACHE=/models
# guard: the face detector needs OpenCV 4.x (headless) with its Haar cascades
RUN python -c "import cv2; assert cv2.__version__.startswith('4.'); cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')"
RUN useradd --create-home --uid 10001 ezra && mkdir -p /data /models && chown ezra /data /models

FROM base AS runtime
USER ezra
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s \
  CMD curl -fsS http://localhost:8000/api/health || exit 1
CMD ["ezra", "serve", "--host", "0.0.0.0", "--port", "8000", "--no-embedded-worker"]

FROM base AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra local
COPY tests ./tests
RUN chown -R ezra /app
USER ezra
ENV HOME=/home/ezra
CMD ["pytest", "-q"]
