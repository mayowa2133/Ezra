"""Transcription providers.

faster-whisper (MIT, default): CTranslate2 Whisper, local, word timestamps,
  batched decoding (~1.8x faster on CPU; see docs/DECISIONS.md).
WhisperX (BSD-2, optional extra `whisperx`): wav2vec2 forced alignment for
  tighter word timing. Heavier (PyTorch). Diarization is handled separately by
  ezra.diarization so either transcriber can be paired with any diarizer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import get_settings
from .base import Progress, Segment, TranscriptionProvider, TranscriptResult, Word, resegment


class FasterWhisperProvider(TranscriptionProvider):
    name = "faster-whisper"

    def __init__(self, model: str | None = None, device: str | None = None, compute: str | None = None,
                 batch: int | None = None):
        s = get_settings()
        self.model = model or s.whisper_model
        self.device = device or s.whisper_device
        self.compute = compute or s.whisper_compute
        self.batch = s.whisper_batch if batch is None else batch

    def settings(self) -> dict[str, Any]:
        return {"model": self.model, "compute": self.compute, "batched": self.batch > 1,
                "prompt": get_settings().whisper_prompt}

    def transcribe(self, media: Path, progress: Progress | None = None) -> TranscriptResult:
        try:
            from faster_whisper import BatchedInferencePipeline, WhisperModel
        except ImportError as e:
            raise RuntimeError("faster-whisper is not installed: `uv sync --extra local`") from e
        model = WhisperModel(self.model, device=self.device, compute_type=self.compute,
                             download_root=str(get_settings().models_dir / "whisper"))
        prompt = get_settings().whisper_prompt
        if self.batch > 1:
            seg_iter, info = BatchedInferencePipeline(model=model).transcribe(
                str(media), word_timestamps=True, batch_size=self.batch, initial_prompt=prompt)
        else:
            seg_iter, info = model.transcribe(str(media), word_timestamps=True, vad_filter=True,
                                              initial_prompt=prompt)
        words: list[Word] = []
        for seg in seg_iter:
            for w in seg.words or []:
                if w.word.strip():
                    words.append(Word(w.word.strip(), round(w.start, 3), round(w.end, 3),
                                      round(float(w.probability), 3)))
            if progress and info.duration:
                progress(min(seg.end / info.duration, 1.0))
        return TranscriptResult(info.language, float(info.duration or 0), resegment(words), self.name, self.model)


class WhisperXProvider(TranscriptionProvider):
    name = "whisperx"

    def __init__(self, model: str | None = None, device: str | None = None, compute: str | None = None):
        s = get_settings()
        self.model = model or s.whisper_model
        self.device = "cpu" if (device or s.whisper_device) == "auto" else (device or s.whisper_device)
        self.compute = compute or s.whisper_compute

    def settings(self) -> dict[str, Any]:
        return {"model": self.model, "compute": self.compute, "aligned": True}

    def transcribe(self, media: Path, progress: Progress | None = None) -> TranscriptResult:
        try:
            import whisperx
        except ImportError as e:
            raise RuntimeError("WhisperX is not installed: `uv sync --extra whisperx` "
                               "(pulls PyTorch; see docs/DEPLOYMENT.md)") from e
        audio = whisperx.load_audio(str(media))
        model = whisperx.load_model(self.model, self.device, compute_type=self.compute,
                                    download_root=str(get_settings().models_dir / "whisper"))
        raw = model.transcribe(audio, batch_size=8)
        if progress:
            progress(0.6)
        align_model, meta = whisperx.load_align_model(language_code=raw["language"], device=self.device)
        aligned = whisperx.align(raw["segments"], align_model, meta, audio, self.device,
                                 return_char_alignments=False)
        words = [Word(w["word"].strip(), round(float(w["start"]), 3), round(float(w["end"]), 3),
                      round(float(w.get("score", 0.0)), 3))
                 for seg in aligned["segments"] for w in seg.get("words", [])
                 if w.get("word", "").strip() and "start" in w and "end" in w]
        duration = len(audio) / 16000.0
        if progress:
            progress(1.0)
        return TranscriptResult(raw["language"], duration, resegment(words), self.name, self.model)


PROVIDERS: dict[str, type[TranscriptionProvider]] = {
    FasterWhisperProvider.name: FasterWhisperProvider,
    WhisperXProvider.name: WhisperXProvider,
}


def get_provider(name: str | None = None) -> TranscriptionProvider:
    name = name or get_settings().transcriber
    if name not in PROVIDERS:
        raise ValueError(f"unknown transcriber {name!r}; available: {sorted(PROVIDERS)}")
    return PROVIDERS[name]()


__all__ = ["FasterWhisperProvider", "WhisperXProvider", "get_provider", "Segment"]
