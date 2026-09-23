"""Local transcription with faster-whisper (the `local` extra). Runs on your
machine; no API key, no upload."""

from __future__ import annotations

from typing import Callable

from . import sources
from .transcript import Transcript, resegment, transcript_path_for
from ..config import settings


def transcribe(source_id: int, progress: Callable[[float], None] | None = None) -> Transcript:
    try:
        from faster_whisper import BatchedInferencePipeline, WhisperModel
    except ImportError as e:  # pragma: no cover - depends on the optional extra
        raise RuntimeError("transcription needs faster-whisper: `uv sync --extra local`") from e

    s = settings()
    src = sources.get(source_id)
    sources.set_status(source_id, "transcribing", error=None)
    try:
        model = WhisperModel(s.whisper_model, device=s.whisper_device, compute_type=s.whisper_compute)
        if s.whisper_batch > 1:
            # ~1.8x faster on a 10-core CPU; word starts agree with sequential decoding
            # to ~10ms median / 40ms p95, well inside the cut padding.
            seg_iter, info = BatchedInferencePipeline(model=model).transcribe(
                src["path"], word_timestamps=True, batch_size=s.whisper_batch)
        else:
            seg_iter, info = model.transcribe(src["path"], word_timestamps=True, vad_filter=True)
        segments = []
        for seg in seg_iter:
            segments.append({
                "start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(),
                "words": [{"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
                          for w in (seg.words or []) if w.word.strip()],
            })
            if progress and info.duration:
                progress(min(seg.end / info.duration, 1.0))
        words = [w for seg in segments for w in seg["words"]]
        t = Transcript(info.language, float(info.duration or src["duration"] or 0),
                       resegment(words) if words else segments)
        path = transcript_path_for(source_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        t.save(path)
        sources.set_status(source_id, "transcribed", transcript_path=str(path))
        return t
    except Exception as e:
        sources.set_status(source_id, "failed", error=str(e)[:1000])
        raise


def import_transcript(source_id: int, transcript: Transcript) -> None:
    """Attach a transcript produced elsewhere (e.g. OpenShorts' metadata)."""
    path = transcript_path_for(source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript.save(path)
    sources.set_status(source_id, "transcribed", transcript_path=str(path), error=None)
