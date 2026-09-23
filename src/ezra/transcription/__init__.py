"""Transcription service: transcribe once, cache forever.

A transcript is keyed by (source, version) where version hashes the provider,
model, decoding settings and diarizer. Asking again with the same settings
returns the stored transcript; changing any of them creates a new version and
keeps the old one."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from sqlalchemy import select

from .. import costs, db, sources
from ..db.models import Transcript, TranscriptSegment
from ..diarization import assign_speakers, get_diarizer
from .base import (Progress, Segment, TranscriptResult, Word, edges, join_words, resegment, snap,
                   window, window_text)
from .providers import get_provider

__all__ = ["ensure_transcript", "latest", "load_words", "load_segments", "Word", "Segment", "snap",
           "window", "window_text", "join_words", "edges", "page", "fmt_ts", "to_dict"]


SEGMENTER_VERSION = "2"   # bump when resegment()/speaker assignment logic changes


def _version(provider_version: str, diarizer: str) -> str:
    return hashlib.sha256(f"{provider_version}|{diarizer}|seg{SEGMENTER_VERSION}".encode()).hexdigest()[:16]


def ensure_transcript(source_id: int, provider: str | None = None, diarizer: str | None = None,
                      force: bool = False, progress: Progress | None = None) -> Transcript:
    tp = get_provider(provider)
    dz = get_diarizer(diarizer)
    version = _version(tp.version(), dz.name)
    with db.session() as s:
        existing = s.scalar(select(Transcript).where(Transcript.source_id == source_id,
                                                     Transcript.version == version))
        if existing and not force:
            return existing
        if existing and force:
            s.delete(existing)
    src = sources.get(source_id)
    media = sources.local_path(src)
    t0 = time.time()
    result: TranscriptResult = tp.transcribe(media, (lambda f: progress(0.8 * f)) if progress else None)
    t_asr = time.time() - t0
    words = result.words
    diar = dz.diarize(media, words)
    assign_speakers(words, diar.turns)
    segments = resegment(words) if diar.turns else result.segments
    if progress:
        progress(0.95)
    with db.session() as s:
        tr = Transcript(source_id=source_id, provider=result.provider, model=result.model, version=version,
                        language=result.language, duration=result.duration or src.duration,
                        word_count=len(words), has_speakers=diar.n_speakers > 0, diarizer=dz.name)
        s.add(tr)
        s.flush()
        for i, seg in enumerate(segments):
            s.add(TranscriptSegment(transcript_id=tr.id, idx=i, start=seg.start, end=seg.end, text=seg.text,
                                    speaker=seg.speaker, confidence=seg.confidence,
                                    sentence_end=seg.sentence_end, words=[w.to_dict() for w in seg.words]))
        tr_id = tr.id
    from ..analysis import store as store_analysis

    store_analysis(source_id, "speakers", dz.name, version, diar.confidence, {
        "n_speakers": diar.n_speakers, "heuristic": dz.name == "local",
        "turns": [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in diar.turns]})
    costs.record("transcription", quantity=time.time() - t0, unit="seconds", source_id=source_id,
                 campaign_id=src.campaign_id, provider=result.provider, model=result.model,
                 asr_seconds=round(t_asr, 1), media_seconds=round(result.duration, 1))
    with db.session() as s:
        return s.get(Transcript, tr_id)  # type: ignore[return-value]


def latest(source_id: int) -> Transcript | None:
    with db.session() as s:
        return s.scalar(select(Transcript).where(Transcript.source_id == source_id)
                        .order_by(Transcript.id.desc()).limit(1))


def load_segments(source_id: int) -> list[Segment]:
    tr = latest(source_id)
    if tr is None:
        raise RuntimeError(f"source {source_id} has no transcript yet (run `ezra analyze`)")
    with db.session() as s:
        rows = s.scalars(select(TranscriptSegment).where(TranscriptSegment.transcript_id == tr.id)
                         .order_by(TranscriptSegment.idx))
        return [Segment(r.start, r.end, r.text, [Word.from_dict(w) for w in r.words], r.speaker,
                        r.confidence, r.sentence_end) for r in rows]


def load_words(source_id: int) -> list[Word]:
    return [w for seg in load_segments(source_id) for w in seg.words]


def fmt_ts(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m):02d}:{s:04.1f}"


def page(source_id: int, start: float = 0.0, max_chars: int = 30000) -> dict[str, Any]:
    """Timestamped, speaker-labelled lines from `start`, cut at ~max_chars so an
    agent can read an hour-long episode in a few calls. next_start is None at the end."""
    tr = latest(source_id)
    lines: list[str] = []
    used = 0
    next_start = None
    for seg in load_segments(source_id):
        if seg.end <= start:
            continue
        who = f"{seg.speaker}: " if seg.speaker else ""
        line = f"[{fmt_ts(seg.start)} → {fmt_ts(seg.end)} | {seg.start:.2f}s] {who}{seg.text}"
        if used + len(line) > max_chars and lines:
            next_start = seg.start
            break
        lines.append(line)
        used += len(line) + 1
    return {"source_id": source_id, "language": tr.language if tr else None,
            "duration": tr.duration if tr else None, "from": start, "next_start": next_start,
            "text": "\n".join(lines)}


def to_dict(tr: Transcript) -> dict[str, Any]:
    return {"id": tr.id, "source_id": tr.source_id, "provider": tr.provider, "model": tr.model,
            "version": tr.version, "language": tr.language, "duration": tr.duration,
            "word_count": tr.word_count, "has_speakers": tr.has_speakers, "diarizer": tr.diarizer,
            "created_at": tr.created_at.isoformat() if tr.created_at else None}
