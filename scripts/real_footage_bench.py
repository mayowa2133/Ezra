"""Real-footage render benchmark: measurable values for every rendered clip of a campaign (or the
given clips), with no ground truth needed. Repeatable: run it before and after a change and diff
the JSON.

Per clip:
  format        1080x1920, H.264/AAC, 30 fps, A/V duration difference
  audio         integrated loudness (LUFS), true peak (dBTP), longest silence left, seconds removed
  captions      caption cues, share of cues over audible sound, first cue vs first sound,
                seconds where the source's own captions are on screen (Ezra's captions hidden there)
  framing       share of time in each layout (track / split / shown whole), crop moves per minute,
                scenes per second (pacing), scenes where graphics forced the crop to move or show whole
  cut quality   opens on a connector/filler or an unresolved pronoun, ends on a sentence end

    uv run python scripts/real_footage_bench.py --campaign mrbeast-test [--out results.json]
    uv run python scripts/real_footage_bench.py --clips 14 15 16
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from pathlib import Path
from typing import Any

from ezra import render
from ezra.benchmark.run import _loudness, _probe, _silences, _srt
from ezra.scoring.features import CONNECTOR_START, DISCOURSE_START, FILLERS, _dangling, _first_words
from ezra.storage import get_storage


def measure_clip(clip: Any) -> dict[str, Any]:
    v = render.current_version(clip)
    st_ = get_storage()
    path = st_.local_path(v.video_key)
    streams = _probe(path)
    vid, aud = streams.get("video", {}), streams.get("audio", {})
    num, den = (int(x) for x in vid.get("r_frame_rate", "0/1").split("/"))
    vdur, adur = float(vid.get("duration", 0)), float(aud.get("duration", 0))
    lufs, peak = _loudness(path)
    quiet = _silences(path, 0.15)
    sound, t = [], 0.0
    for a, b in quiet:
        if a > t:
            sound.append((t, a))
        t = b
    if t < vdur:
        sound.append((t, vdur))
    cues = _srt(st_.local_path(v.srt_key)) if v.srt_key else []
    over = [any(a <= (c0 + c1) / 2 <= b for a, b in sound) for c0, c1, _ in cues]
    long_gaps = [b - a for a, b in _silences(path, 1.0) if b != float("inf")]
    summ = v.edit_summary or {}
    scenes = summ.get("scenes", [])
    dur = max(1e-6, v.duration or vdur)
    share = {m: round(sum(s["end"] - s["start"] for s in scenes if s["mode"] == m) / dur, 3)
             for m in ("track", "center", "split", "blur")}
    moves = sum(max(0, len(s.get("shots", [])) - 1) for s in scenes)
    cand = clip.candidate
    first = _first_words(cand.transcript or "", 3)
    return {
        "clip_id": clip.id, "title": clip.title, "duration": round(dur, 2),
        "format_ok": (vid.get("width"), vid.get("height")) == (1080, 1920) and vid.get("codec_name") == "h264"
        and aud.get("codec_name") == "aac" and den > 0 and abs(num / den - 30) < 0.01,
        "av_diff_s": round(abs(vdur - adur), 3), "lufs": lufs, "true_peak_dbtp": peak,
        "longest_silence_s": round(max(long_gaps), 2) if long_gaps else 0.0,
        "removed_s": summ.get("removed_seconds"),
        "cues": len(cues), "cues_over_sound": round(sum(over) / len(over), 3) if over else None,
        "first_cue_minus_first_sound_s": round(cues[0][0] - sound[0][0], 3) if cues and sound else None,
        "source_caption_s": round(sum(b - a for a, b in summ.get("source_captions") or []), 2),
        "layout_share": share, "crop_moves_per_min": round(moves / (dur / 60), 2),
        "scenes_per_s": round(len(scenes) / dur, 3),
        "graphics_moved": sum(1 for s in scenes if s.get("protected") == "moved"),
        "graphics_shown_whole": sum(1 for s in scenes if s.get("protected") == "shown whole"),
        "opens_mid_thought": bool(first) and (first[0] in CONNECTOR_START or first[0] in FILLERS
                                              or " ".join(first[:2]) in DISCOURSE_START),
        "opens_on_unresolved_pronoun": _dangling(first),
        "ends_on_sentence": (cand.transcript or "").rstrip()[-1:] in ".!?\"'",
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def med(k: str) -> float | None:
        xs = [r[k] for r in rows if isinstance(r.get(k), int | float)]
        return round(st.median(xs), 3) if xs else None

    n = len(rows)
    return {
        "clips": n,
        "format_ok": sum(r["format_ok"] for r in rows),
        "loudness_within_1lu": sum(1 for r in rows if r["lufs"] is not None and abs(r["lufs"] + 14) <= 1),
        "true_peak_ok": sum(1 for r in rows if r["true_peak_dbtp"] is not None and r["true_peak_dbtp"] <= -1),
        "av_diff_max_s": max((r["av_diff_s"] for r in rows), default=None),
        "median_duration_s": med("duration"), "median_longest_silence_s": med("longest_silence_s"),
        "median_cues_over_sound": med("cues_over_sound"),
        "whole_frame_share": round(st.mean(r["layout_share"]["blur"] for r in rows), 3) if rows else None,
        "split_share": round(st.mean(r["layout_share"]["split"] for r in rows), 3) if rows else None,
        "median_crop_moves_per_min": med("crop_moves_per_min"), "median_scenes_per_s": med("scenes_per_s"),
        "clips_with_source_captions": sum(1 for r in rows if r["source_caption_s"] > 0),
        "opens_mid_thought": sum(r["opens_mid_thought"] for r in rows),
        "opens_on_unresolved_pronoun": sum(r["opens_on_unresolved_pronoun"] for r in rows),
        "ends_on_sentence": sum(r["ends_on_sentence"] for r in rows),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign")
    ap.add_argument("--clips", type=int, nargs="*")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    clips = [render.get_clip(i) for i in a.clips] if a.clips else [
        c for c in render.list_clips(a.campaign) if render.current_version(c) is not None]
    rows = [measure_clip(c) for c in clips if render.current_version(c) and render.current_version(c).video_key]
    report = {"summary": summarize(rows), "clips": rows}
    text = json.dumps(report, indent=2, default=str)
    if a.out:
        a.out.write_text(text)
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
