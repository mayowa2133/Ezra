"""End-to-end quality benchmark on the synthetic fixtures.

    ezra benchmark --out benchmarks/results --fixtures solo,podcast,multi,scenes

Each fixture runs through the real pipeline (ingest → analyze → candidates →
render) in its own throwaway EZRA_HOME and is scored against the fixture's
ground truth (benchmark/fixtures.py). Two deterministic suites follow:
compliance cases with known answers and job-queue retry behaviour.

Output: <out>/<timestamp>/report.json (machine-readable) and report.md.

What the numbers can and cannot say: the fixtures are synthetic (TTS voices,
still portraits), so transcription and diarization are easier than on real
podcasts, and "strong moment" labels are the fixture author's judgement.
They are regression signals, not claims about real-world virality.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from . import fixtures as fx

TOP_N = 3            # clips rendered per fixture
OPENING_WORDS = 30   # words that make up a clip's opening
SCENE_TOL = 0.5      # seconds: a detected cut within this of a true cut is a hit

# Author-labelled moments of the fixture script: (label, words that identify it).
STRONG = [
    ("lost $400k to a fraudulent vendor", ["overnight", "payroll"]),
    ("too proud to ask for help", ["proud"]),
    ("almost quit, resignation letter", ["resignation"]),
    ("raised too early, it made us lazy", ["lazy"]),
    ("most startups should never raise venture money", ["never", "venture"]),
    ("sell before you build", ["sell", "paid"]),
]
DULL = [
    ("hiring platitudes", ["culture", "values"]),
    ("the tools we use", ["spreadsheets"]),
    ("lightning round", ["coffee"]),
]

BENCH_CAMPAIGN = """
campaign:
  id: bench
  name: Benchmark
  cpm: 2.0
  minimum_views: 5000
  maximum_payout: 1000
  budget: 5000
  platforms: [tiktok, instagram, youtube]
  min_duration: 15
  max_duration: 60
  hashtags: ["#founderstories"]
  subtitles_required: true
  forbidden: [profanity, competitor mentions]
  competitors: [Acme Ventures]
  brief: Founder stories with real stakes - money lost, near quitting, hard lessons, contrarian takes on fundraising.
"""
STRICT_CAMPAIGN = BENCH_CAMPAIGN.replace("id: bench", "id: strict").replace(
    "brief:", "forbidden_topics: [politics]\n  brief:")


# --- text helpers --------------------------------------------------------------------------------

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _num_words(n: int) -> list[str]:
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [_ONES[n // 100], "hundred"] + (_num_words(n % 100) if n % 100 else [])
    for size, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= size:
            return _num_words(n // size) + [name] + (_num_words(n % size) if n % size else [])
    return [str(n)]


def _year_words(n: int) -> list[str]:
    """2019 → twenty nineteen (how the fixture script says years)."""
    hi, lo = divmod(n, 100)
    if 2000 <= n <= 2009:
        return _num_words(n)
    if lo == 0:
        return _num_words(hi) + ["hundred"]
    return _num_words(hi) + (_num_words(lo) if lo >= 10 else ["oh", *_num_words(lo)])


def normalize(text: str) -> list[str]:
    """Lower-case words with numbers spelled out, so '$400,000' matches 'four hundred thousand dollars'."""
    out: list[str] = []
    for tok in re.findall(r"\$?[\d,]*\d(?:\.\d+)?%?|[a-z']+", text.lower().replace("-", " ")):
        if tok[0].isdigit() or tok[0] == "$":
            dollars, pct = tok.startswith("$"), tok.endswith("%")
            digits = tok.strip("$%").replace(",", "")
            if "." in digits:
                digits = digits.split(".")[0]
            n = int(digits)
            words = _year_words(n) if 1900 <= n <= 2099 and not dollars and "," not in tok else _num_words(n)
            out += words + (["dollars"] if dollars else []) + (["percent"] if pct else [])
        else:
            w = tok.strip("'")
            if w:
                out.append(w)
    return out


def wer(ref: list[str], hyp: list[str]) -> dict[str, Any]:
    """Word error rate with substitution / deletion / insertion counts (Levenshtein)."""
    n, m = len(ref), len(hyp)
    d = np.zeros((n + 1, m + 1), dtype=np.int32)
    d[:, 0] = np.arange(n + 1)
    d[0, :] = np.arange(m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]))
    i, j, sub, dele, ins = n, m, 0, 0, 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i, j] == d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]):
            sub += ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i > 0 and d[i, j] == d[i - 1, j] + 1:
            dele += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return {"wer": round(float(d[n, m]) / max(1, n), 4), "ref_words": n, "hyp_words": m,
            "substitutions": sub, "deletions": dele, "insertions": ins}


def _has(tokens: list[str], keys: list[str]) -> bool:
    s = set(tokens)
    return all(k in s for k in keys)


def _pct(xs: list[float], q: float) -> float | None:
    return round(float(np.percentile(xs, q)), 3) if xs else None


# --- media helpers --------------------------------------------------------------------------------

def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,r_frame_rate,duration,sample_rate,channels",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return {s["codec_type"]: s for s in json.loads(out.stdout)["streams"]}


def _silences(path: Path, min_len: float) -> list[tuple[float, float]]:
    out = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af",
                          f"silencedetect=noise=-35dB:d={min_len}", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True, check=False)
    starts = [max(0.0, float(x)) for x in re.findall(r"silence_start: (-?[\d.]+)", out.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out.stderr)]
    if len(ends) < len(starts):
        ends.append(float("inf"))
    return list(zip(starts, ends, strict=False))


def _loudness(path: Path) -> tuple[float | None, float | None]:
    """Integrated loudness (LUFS) and true peak (dBTP) per EBU R128."""
    out = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True, check=False)
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", out.stderr)
    summary = out.stderr[out.stderr.rfind("Summary"):]
    peak = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    return (float(found[-1]) if found else None,
            float(peak[-1]) if peak and peak[-1] != "-inf" else None)


def _srt(path: Path) -> list[tuple[float, float, str]]:
    def t(x: str) -> float:
        h, m, rest = x.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest.replace(",", "."))

    cues = []
    for block in path.read_text().strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 3 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            cues.append((t(a), t(b), " ".join(lines[2:])))
    return cues


# --- per-fixture evaluation -----------------------------------------------------------------------

def _truth_speaker(turns: list[dict[str, Any]], t: float) -> str | None:
    best, dist = None, 1e9
    for tr in turns:
        if tr["start"] <= t <= tr["end"]:
            return str(tr["speaker"])
        d = min(abs(t - tr["start"]), abs(t - tr["end"]))
        if d < dist:
            best, dist = tr["speaker"], d
    return str(best) if dist < 1.0 else None


def eval_transcript(words: list[Any], truth: dict[str, Any]) -> dict[str, Any]:
    ref = normalize(" ".join(t["text"] for t in truth["turns"]))
    hyp = normalize(" ".join(w.w for w in words))
    res = wer(ref, hyp)
    onset = [min(abs(w.s - tr["start"]) for w in words) for tr in truth["turns"]] if words else []
    offset = [min(abs(w.e - tr["end"]) for w in words) for tr in truth["turns"]] if words else []
    res.update(turn_onset_error_median=_pct(onset, 50), turn_onset_error_p90=_pct(onset, 90),
               turn_offset_error_median=_pct(offset, 50), turn_offset_error_p90=_pct(offset, 90))
    fillers = [w for w in hyp if w in ("um", "uh")]
    res["fillers_in_script"] = sum(1 for w in ref if w in ("um", "uh"))
    res["fillers_transcribed"] = len(fillers)
    return res


def eval_diarization(words: list[Any], truth: dict[str, Any]) -> dict[str, Any]:
    from scipy.optimize import linear_sum_assignment

    pairs = [(w.spk, _truth_speaker(truth["turns"], (w.s + w.e) / 2)) for w in words]
    pairs = [(h, r) for h, r in pairs if h and r]
    hyps, refs = sorted({str(h) for h, _ in pairs}), sorted({str(r) for _, r in pairs})
    if not pairs:
        return {"word_accuracy": None, "speakers_true": len(truth["speakers"]), "speakers_found": 0}
    counts = Counter(pairs)
    cost = np.array([[-counts.get((h, r), 0) for r in refs] for h in hyps])
    rows, cols = linear_sum_assignment(cost)
    matched = -int(cost[rows, cols].sum())
    return {"word_accuracy": round(matched / len(pairs), 4), "words": len(pairs),
            "speakers_true": len(truth["speakers"]), "speakers_found": len(hyps),
            "mapping": {hyps[r]: refs[c] for r, c in zip(rows, cols, strict=True)}}


def eval_scenes(scenes: list[dict[str, Any]], truth: dict[str, Any]) -> dict[str, Any]:
    found = [float(s["start"]) for s in scenes[1:]]
    true = [float(c) for c in truth.get("scene_cuts", [])]
    unmatched, hits = list(true), 0
    for c in found:
        near = min(unmatched, key=lambda t: abs(t - c), default=None)
        if near is not None and abs(near - c) <= SCENE_TOL:
            hits += 1
            unmatched.remove(near)
    precision = hits / len(found) if found else (1.0 if not true else 0.0)
    recall = hits / len(true) if true else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"true_cuts": len(true), "found_cuts": len(found), "hits": hits, "precision": round(precision, 3),
            "recall": round(recall, 3), "f1": round(f1, 3), "false_cuts": len(found) - hits}


def _shot_at(truth: dict[str, Any], t: float) -> dict[str, Any] | None:
    return next((s for s in truth["shots"] if s["start"] <= t < s["end"]), None)


def eval_faces(faces: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    """A detection counts when its centre falls inside a true portrait tile."""
    aspect = truth["width"] / truth["height"]
    tp = fn = fp = 0
    for t, dets in faces.get("timeline", []):
        shot = _shot_at(truth, t)
        if shot is None:
            continue
        true_faces = list(shot["faces"])
        for cx, cy, _w, _h in dets:
            hit = next((f for f in true_faces if abs(cx - f["cx"]) <= f["size"] / 2
                        and abs(cy - f["cy"]) <= f["size"] * aspect / 2), None)
            if hit is not None:
                tp += 1
                true_faces.remove(hit)
            else:
                fp += 1
        fn += len(true_faces)
    return {"samples": len(faces.get("timeline", [])), "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None, "false_positives": fp,
            "layout_hints": sorted({s["layout_hint"] for s in faces.get("scenes", [])})}


def eval_candidates(cands: list[Any], words: list[Any], camp: Any, top: int = 5) -> dict[str, Any]:
    from ..transcription.base import ends_sentence

    starts = {round(w.s, 2) for i, w in enumerate(words) if i == 0 or ends_sentence(words[i - 1].w)}

    def clean_start(c: Any) -> bool:
        first = next((w for w in words if w.s >= c.start - 0.3), None)
        return first is not None and round(first.s, 2) in starts

    def clean_end(c: Any) -> bool:
        inside = [w for w in words if w.s < c.end and w.e > c.start]
        return bool(inside) and ends_sentence(inside[-1].w)

    ranked = sorted(cands, key=lambda c: -(c.rank_score or 0))
    topk = ranked[:top]
    toks = [normalize(c.transcript or "") for c in topk]
    # a clip is credited with the moment it opens on (its first ~2 sentences), not
    # everything a long window happens to contain
    openings = [t[:OPENING_WORDS] for t in toks]
    strong = sorted({label for label, keys in STRONG for o in openings if _has(o, keys)})
    dull = [i + 1 for i, t in enumerate(toks) if any(_has(t, keys) for _, keys in DULL)]
    ious = []
    for a, b in combinations(topk, 2):
        inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
        union = max(a.end, b.end) - min(a.start, b.start)
        ious.append(inter / union if union else 0.0)
    lo, hi = (camp.min_duration or 0), (camp.max_duration or 1e9)
    n = max(1, len(cands))
    return {
        "count": len(cands),
        "clean_start_rate": round(sum(map(clean_start, cands)) / n, 3),
        "clean_end_rate": round(sum(map(clean_end, cands)) / n, 3),
        "ends_on_question_rate": round(sum((c.transcript or "").rstrip().endswith("?") for c in cands) / n, 3),
        "duration_in_bounds_rate": round(sum(lo <= c.end - c.start <= hi for c in cands) / n, 3),
        "compliance": dict(Counter(c.compliance_status for c in cands)),
        f"strong_recall_at_{top}": round(len(strong) / len(STRONG), 3),
        f"strong_found_at_{top}": strong,
        "top1_opens_on_strong_moment": bool(openings) and any(_has(openings[0], k) for _, k in STRONG),
        f"top_{top}_ranks_containing_dull_material": dull,
        f"mean_pairwise_iou_top_{top}": round(float(np.mean(ious)), 3) if ious else 0.0,
        "top": [{"rank_score": c.rank_score, "hook_type": c.hook_type, "start": round(c.start, 2),
                 "duration": round(c.end - c.start, 1), "opens": " ".join((c.transcript or "").split()[:14])}
                for c in topk],
    }


def eval_render(clip: Any, version: Any, st: Any, truth: dict[str, Any], src_start: float,
                src_end: float) -> dict[str, Any]:
    path = st.local_path(version.video_key)
    streams = _probe(path)
    v, a = streams.get("video", {}), streams.get("audio", {})
    num, den = (int(x) for x in v.get("r_frame_rate", "0/1").split("/"))
    vdur, adur = float(v.get("duration", 0)), float(a.get("duration", 0))
    lufs, true_peak = _loudness(path)
    silences = _silences(path, 0.15)
    regions: list[tuple[float, float]] = []
    t = 0.0
    for s, e in silences:
        if s > t:
            regions.append((t, s))
        t = e
    if t < vdur:
        regions.append((t, vdur))
    # a sound shorter than 100 ms before the first words is a blip (e.g. the tail of
    # the previous word left in by the cut), not speech
    leading_blip = bool(regions) and regions[0][1] - regions[0][0] < 0.1 and len(regions) > 1
    speech = [r for r in regions if r[1] - r[0] >= 0.1]
    cues = _srt(st.local_path(version.srt_key)) if version.srt_key else []
    over = [any(s <= (c0 + c1) / 2 <= e for s, e in speech) for c0, c1, _ in cues]
    first_speech = speech[0][0] if speech else None
    long_gaps = [e - s for s, e in _silences(path, 1.0) if e != float("inf")]
    summary = version.edit_summary or {}

    # crop stability + correctness against the true faces in this clip's source range
    shots = [sh for sc in summary.get("scenes", []) for sh in sc.get("shots", [])]
    moves = sum(max(0, len(sc.get("shots", [])) - 1) for sc in summary.get("scenes", []))
    faces_here = [f for s in truth["shots"] if s["start"] < src_end and s["end"] > src_start for f in s["faces"]]
    on_face = [any(abs(x - f["cx"]) <= f["size"] / 2 for f in faces_here) for _, _, x in shots]
    split = [sc["split_x"] for sc in summary.get("scenes", []) if sc.get("split_x")]
    split_err = []
    for pair in split:
        xs = sorted(f["cx"] for f in faces_here)
        if len(xs) >= 2:
            split_err.append(max(abs(sorted(pair)[0] - xs[0]), abs(sorted(pair)[1] - xs[-1])))
    checks = {
        "resolution_1080x1920": (v.get("width"), v.get("height")) == (1080, 1920),
        "h264_aac": v.get("codec_name") == "h264" and a.get("codec_name") == "aac",
        "fps_30": den > 0 and abs(num / den - 30) < 0.01,
        "av_sync_under_100ms": abs(vdur - adur) < 0.1,
        "loudness_within_1lu_of_-14": lufs is not None and abs(lufs + 14) <= 1,
        "true_peak_at_most_-1dbtp": true_peak is not None and true_peak <= -1.0,
        "captions_over_speech_90pct": bool(over) and sum(over) / len(over) >= 0.9,
        "first_caption_within_400ms": bool(cues) and first_speech is not None and abs(cues[0][0] - first_speech) < 0.4,
        "no_silence_over_1s": not long_gaps,
        "no_blip_before_first_words": not leading_blip,
    }
    return {
        "clip_id": clip.id, "layout": version.layout_used, "duration": round(version.duration or 0, 2),
        "render_seconds": round(version.render_seconds or 0, 1),
        "width": v.get("width"), "height": v.get("height"), "fps": round(num / den, 3) if den else None,
        "av_duration_diff": round(abs(vdur - adur), 3), "loudness_lufs": lufs, "true_peak_dbtp": true_peak,
        "captions": len(cues), "captions_over_speech": round(sum(over) / len(over), 3) if over else None,
        "first_caption_minus_first_speech": round(cues[0][0] - first_speech, 3) if cues and first_speech is not None
        else None,
        "removed_seconds": summary.get("removed_seconds"), "leading_blip": leading_blip,
        "boundaries_refined": summary.get("boundaries_refined"), "longest_silence_left": round(max(long_gaps), 2)
        if long_gaps else 0.0,
        "crop_moves_per_min": round(moves / max(1e-6, (version.duration or 1) / 60), 2),
        "track_shots_on_a_face": round(sum(on_face) / len(on_face), 3) if on_face else None,
        "split_center_error": round(max(split_err), 3) if split_err else None,
        "checks": checks, "passed": all(checks.values()),
    }


# --- deterministic suites --------------------------------------------------------------------------

def bench_compliance() -> dict[str, Any]:
    from .. import campaigns, compliance

    campaigns.import_text(BENCH_CAMPAIGN)
    campaigns.import_text(STRICT_CAMPAIGN)
    basic, strict = campaigns.get("bench"), campaigns.get("strict")
    ok_src = SimpleNamespace(rights_status="authorized", rights_basis="campaign_supplied", rights_notes=None)
    unverified = SimpleNamespace(rights_status="unverified", rights_basis="unknown", rights_notes=None)
    rejected = SimpleNamespace(rights_status="rejected", rights_basis="unknown", rights_notes="no licence")
    swear = sorted(compliance.PROFANITY)[0]
    story = "We lost the money overnight and it changed how we run the company."
    topic_rules = [r.id for r in strict.rules if r.kind in ("forbidden_topic", "freeform")]
    yes = {str(i): {"compliant": True, "reason": "no political content"} for i in topic_rules}
    no = {str(i): {"compliant": False, "reason": "discusses an election"} for i in topic_rules}
    cases: list[tuple[str, Any, str, dict[str, Any], str]] = [
        ("clean candidate", basic, "candidate", dict(duration=30, transcript=story, source=ok_src), "PASS"),
        ("too short (8s)", basic, "candidate", dict(duration=8, transcript=story, source=ok_src), "FAIL"),
        ("too long (75s)", basic, "candidate", dict(duration=75, transcript=story, source=ok_src), "FAIL"),
        ("profanity", basic, "candidate", dict(duration=30, transcript=f"this is {swear} honestly", source=ok_src),
         "FAIL"),
        ("competitor mention", basic, "candidate",
         dict(duration=30, transcript="Acme Ventures offered us a term sheet.", source=ok_src), "FAIL"),
        ("unverified source rights", basic, "candidate", dict(duration=30, transcript=story, source=unverified),
         "REVIEW_REQUIRED"),
        ("rejected source rights", basic, "candidate", dict(duration=30, transcript=story, source=rejected), "FAIL"),
        ("forbidden topic, no model verdict", strict, "candidate",
         dict(duration=30, transcript=story, source=ok_src), "REVIEW_REQUIRED"),
        ("forbidden topic, keyword hit", strict, "candidate",
         dict(duration=30, transcript="Honestly the politics of this election scared investors.", source=ok_src),
         "REVIEW_REQUIRED"),
        ("forbidden topic, model says compliant", strict, "candidate",
         dict(duration=30, transcript=story, source=ok_src, llm_rule_verdicts=yes), "PASS"),
        ("forbidden topic, model says violation", strict, "candidate",
         dict(duration=30, transcript=story, source=ok_src, llm_rule_verdicts=no), "REVIEW_REQUIRED"),
        ("render without subtitles", basic, "render", dict(render_spec={"captions": False}), "FAIL"),
        ("render with subtitles", basic, "render", dict(render_spec={"captions": True}), "PASS"),
        ("publish copy missing hashtag", basic, "publish",
         dict(copy_text="We lost it all", platforms=["tiktok"], posting_counts={"day_total": 0}), "FAIL"),
        ("publish copy ok", basic, "publish",
         dict(copy_text="We lost it all #founderstories", platforms=["tiktok"], posting_counts={"day_total": 0}),
         "PASS"),
        ("publish to a platform outside the campaign", basic, "publish",
         dict(copy_text="#founderstories", platforms=["linkedin"], posting_counts={"day_total": 0}), "FAIL"),
        ("publish copy mentions competitor", basic, "publish",
         dict(copy_text="Better than Acme Ventures #founderstories", platforms=["tiktok"]), "FAIL"),
    ]
    rows = []
    for name, camp, stage, ev, expected in cases:
        got = compliance.evaluate(camp, stage, **ev)
        rows.append({"case": name, "stage": stage, "expected": expected, "got": got["status"],
                     "ok": got["status"] == expected, "reasons": [r["message"] for r in got["reasons"]]})
    return {"cases": rows, "passed": sum(r["ok"] for r in rows), "total": len(rows)}


def bench_jobs() -> dict[str, Any]:
    from sqlalchemy import update

    from .. import db, jobs
    from ..db.models import Job

    calls = {"flaky": 0}

    @jobs.task("bench_flaky")
    def flaky(ctx: Any) -> dict[str, Any]:
        calls["flaky"] += 1
        if calls["flaky"] < 3:
            raise RuntimeError(f"transient failure {calls['flaky']}")
        return {"ok": True}

    @jobs.task("bench_permanent")
    def permanent(ctx: Any) -> dict[str, Any]:
        raise ValueError("bad input: not retryable")

    def drain(job_id: int, limit: int = 10) -> Any:
        delays: list[int] = []
        for _ in range(limit):
            job = jobs.claim(job_id)
            if job is None:
                break
            job = jobs.run(job)
            if job.status != "queued":
                return job, delays
            delays.append(round((db.aware(job.run_after) - jobs.utcnow()).total_seconds()))  # type: ignore[operator]
            with db.session() as s:   # skip the backoff wait
                s.execute(update(Job).where(Job.id == job_id).values(run_after=jobs.utcnow()))
        return jobs.get(job_id), delays

    out: dict[str, Any] = {}
    j, delays = drain(jobs.enqueue("bench_flaky", {}, max_attempts=3).id)
    out["transient_error_retried"] = {"status": j.status, "attempts": j.attempts, "backoff_seconds": delays,
                                      "ok": j.status == "completed" and j.attempts == 3}
    calls["flaky"] = -10
    j, _ = drain(jobs.enqueue("bench_flaky", {}, max_attempts=3).id)
    out["gives_up_after_max_attempts"] = {"status": j.status, "attempts": j.attempts,
                                          "ok": j.status == "failed" and j.attempts == 3}
    j, _ = drain(jobs.enqueue("bench_permanent", {}).id)
    out["permanent_error_not_retried"] = {"status": j.status, "attempts": j.attempts,
                                          "ok": j.status == "failed" and j.attempts == 1}
    q = jobs.enqueue("noop", {})
    jobs.cancel(q.id)
    out["cancelled_job_not_claimed"] = {"status": jobs.get(q.id).status,
                                        "ok": jobs.get(q.id).status == "cancelled" and jobs.claim(q.id) is None}
    r = jobs.claim(jobs.enqueue("noop", {}).id)
    assert r is not None
    requeued = jobs.requeue_stale(jobs.utcnow() + timedelta(minutes=10))
    out["stale_running_job_requeued"] = {"requeued": requeued, "ok": r.id in requeued
                                         and jobs.get(r.id).status == "queued"}
    a, b = jobs.enqueue("noop", {}, dedupe_key="same"), jobs.enqueue("noop", {}, dedupe_key="same")
    out["duplicate_enqueue_deduplicated"] = {"ok": a.id == b.id}
    out["passed"] = sum(v["ok"] for v in out.values() if isinstance(v, dict))
    out["total"] = sum(1 for v in out.values() if isinstance(v, dict))
    return out


# --- orchestration -----------------------------------------------------------------------------------

class _Home:
    """Point Ezra at a throwaway EZRA_HOME (SQLite + local storage) for one suite."""

    KEYS = ("EZRA_HOME", "EZRA_DATABASE_URL", "EZRA_STORAGE", "EZRA_SECRET_KEY", "EZRA_JOB_ISOLATION",
            "EZRA_MODEL_CACHE", "EZRA_LLM")

    def __init__(self, home: Path):
        self.home, self.saved = home, {k: os.environ.get(k) for k in self.KEYS}

    def __enter__(self) -> _Home:
        from .. import db, storage
        from ..config import reset_settings

        os.environ["EZRA_HOME"] = str(self.home)
        for k in ("EZRA_DATABASE_URL", "EZRA_STORAGE", "EZRA_LLM"):
            os.environ.pop(k, None)
        os.environ.setdefault("EZRA_SECRET_KEY", "benchmark-only")
        os.environ["EZRA_JOB_ISOLATION"] = "false"
        os.environ.setdefault("EZRA_MODEL_CACHE", str(Path.home() / ".cache" / "ezra" / "models"))
        reset_settings()
        storage._storage = None
        db.migrate()
        return self

    def __exit__(self, *exc: Any) -> None:
        from .. import storage
        from ..config import reset_settings

        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_settings()
        storage._storage = None


def bench_fixture(name: str, fixture_dir: Path, home: Path, say: Any) -> dict[str, Any]:
    from .. import analysis, campaigns, candidates, render, sources, transcription
    from ..config import get_settings
    from ..storage import get_storage

    video, truth_path = fx.build(name, fixture_dir)
    truth = json.loads(truth_path.read_text())
    with _Home(home):
        campaigns.import_text(BENCH_CAMPAIGN)
        camp = campaigns.get("bench")
        src = sources.ingest(video, "bench", rights_basis="campaign_supplied")
        t0 = time.time()
        analysis.analyze_source(src.id)
        t_analyze = time.time() - t0
        t0 = time.time()
        candidates.find_candidates(src.id, "bench")
        t_find = time.time() - t0
        words = transcription.load_words(src.id)
        products = analysis.all_for(src.id)
        cands = candidates.list_candidates("bench", include_failed=True)
        say(f"  {name}: analyzed in {t_analyze:.0f}s, {len(cands)} candidates; rendering top {TOP_N}")
        t0 = time.time()
        clips = render.render_top("bench", top=TOP_N)
        t_render = time.time() - t0
        st = get_storage()
        renders = []
        for clip in clips:
            v = render.current_version(clip)
            cand = candidates.get(clip.candidate_id)
            if v is not None:
                renders.append(eval_render(clip, v, st, truth, cand.start, cand.end))
        dur = truth["duration"]
        return {
            "fixture": name, "duration": dur, "tts": truth.get("engine"),
            "whisper_model": get_settings().whisper_model, "diarizer": get_settings().diarizer,
            "face_detector": get_settings().face_detector,
            "timing": {"analyze_s": round(t_analyze, 1), "candidates_s": round(t_find, 1),
                       "render_top_s": round(t_render, 1),
                       "analyze_realtime_factor": round(t_analyze / dur, 3)},
            "transcript": eval_transcript(words, truth),
            "diarization": eval_diarization(words, truth),
            "scenes": eval_scenes(products["scenes"].data["scenes"], truth) if "scenes" in products else None,
            "faces": eval_faces(products["faces"].data, truth) if "faces" in products else None,
            "candidates": eval_candidates(cands, words, camp),
            "renders": renders,
        }


def run_benchmark(out: Path, fixtures: tuple[str, ...] = ("solo", "podcast", "multi", "scenes"),
                  console: Any = None, fixture_dir: Path | None = None) -> Path:
    say = console.print if console is not None else print
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (out / stamp).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir = fixture_dir or Path(os.environ.get("EZRA_FIXTURE_CACHE",
                                                     Path.home() / ".cache" / "ezra" / "fixtures"))
    report: dict[str, Any] = {
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "machine": {"platform": platform.platform(), "python": platform.python_version(),
                    "cpu_count": os.cpu_count()},
        "fixtures": {}, "compliance": None, "jobs": None,
    }
    t_all = time.time()
    for name in fixtures:
        say(f"fixture {name}")
        report["fixtures"][name] = bench_fixture(name, fixture_dir, run_dir / "homes" / name, say)
    with _Home(run_dir / "homes" / "_suites"):
        say("compliance cases")
        report["compliance"] = bench_compliance()
        say("job retry behaviour")
        report["jobs"] = bench_jobs()
    report["total_seconds"] = round(time.time() - t_all, 1)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (run_dir / "report.md").write_text(to_markdown(report))
    return run_dir / "report.md"


# --- report ------------------------------------------------------------------------------------------

def _fmt(x: Any) -> str:
    if x is None:
        return "–"
    if isinstance(x, bool):
        return "yes" if x else "**no**"
    if isinstance(x, float):
        return f"{x:.3f}".rstrip("0").rstrip(".") if not math.isnan(x) else "–"
    return str(x)


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_fmt(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def to_markdown(r: dict[str, Any]) -> str:
    fx_ = r["fixtures"]
    names = list(fx_)
    out = [f"# Ezra benchmark: {r['started']}", "",
           f"Machine: {r['machine']['platform']}, Python {r['machine']['python']}, "
           f"{r['machine']['cpu_count']} CPUs. Total {r['total_seconds']}s.", ""]
    if names:
        first = fx_[names[0]]
        out += [f"Providers: whisper `{first['whisper_model']}`, diarizer `{first['diarizer']}`, "
                f"faces `{first['face_detector']}`, scoring heuristic (no model).", ""]
        out += ["## Speed", "", _table(
            ["fixture", "duration s", "analyze s", "× realtime", "candidates s", f"render top {TOP_N} s"],
            [[n, fx_[n]["duration"], fx_[n]["timing"]["analyze_s"], fx_[n]["timing"]["analyze_realtime_factor"],
              fx_[n]["timing"]["candidates_s"], fx_[n]["timing"]["render_top_s"]] for n in names]), ""]
        out += ["## Transcription and speakers", "", _table(
            ["fixture", "WER", "sub/del/ins", "turn onset err p50/p90 s", "diarization word acc",
             "speakers found/true"],
            [[n, fx_[n]["transcript"]["wer"],
              f"{fx_[n]['transcript']['substitutions']}/{fx_[n]['transcript']['deletions']}/"
              f"{fx_[n]['transcript']['insertions']}",
              f"{_fmt(fx_[n]['transcript']['turn_onset_error_median'])}/"
              f"{_fmt(fx_[n]['transcript']['turn_onset_error_p90'])}",
              fx_[n]["diarization"]["word_accuracy"],
              f"{fx_[n]['diarization']['speakers_found']}/{fx_[n]['diarization']['speakers_true']}"]
             for n in names]), ""]
        out += ["## Scenes and faces", "", _table(
            ["fixture", "true cuts", "found", "precision", "recall", "face recall", "face precision",
             "false faces", "layout hints"],
            [[n, s["true_cuts"], s["found_cuts"], s["precision"], s["recall"], f_["recall"], f_["precision"],
              f_["false_positives"], ", ".join(f_["layout_hints"])]
             for n in names for s, f_ in [(fx_[n]["scenes"] or {}, fx_[n]["faces"] or {})]]), ""]
        k = 5
        out += ["## Candidates", "", _table(
            ["fixture", "count", "clean start", "clean end", "ends on ?", "in duration bounds",
             f"strong openings@{k}", "#1 opens strong", f"top {k} with dull material", f"mean IoU top {k}"],
            [[n, c["count"], c["clean_start_rate"], c["clean_end_rate"], c["ends_on_question_rate"],
              c["duration_in_bounds_rate"], c[f"strong_recall_at_{k}"], c["top1_opens_on_strong_moment"],
              len(c[f"top_{k}_ranks_containing_dull_material"]), c[f"mean_pairwise_iou_top_{k}"]]
             for n in names for c in [fx_[n]["candidates"]]]), ""]
        for n in names:
            out += [f"Top {k} on `{n}`:", ""]
            out += [f"{i + 1}. {t['rank_score']} ({t['hook_type']}, {t['duration']}s): {t['opens']}…"
                    for i, t in enumerate(fx_[n]["candidates"]["top"])]
            out += [""]
        out += ["## Renders", "", _table(
            ["fixture", "clip", "layout", "dur s", "render s", "A/V diff s", "LUFS", "dBTP", "captions over speech",
             "1st caption − speech s", "silence removed s", "crop moves/min", "track on face", "split err",
             "all checks"],
            [[n, x["clip_id"], x["layout"], x["duration"], x["render_seconds"], x["av_duration_diff"],
              x["loudness_lufs"], x["true_peak_dbtp"], x["captions_over_speech"], x["first_caption_minus_first_speech"],
              x["removed_seconds"], x["crop_moves_per_min"], x["track_shots_on_a_face"], x["split_center_error"],
              x["passed"]] for n in names for x in fx_[n]["renders"]]), ""]
        failed = [(n, x["clip_id"], c) for n in names for x in fx_[n]["renders"]
                  for c, ok in x["checks"].items() if not ok]
        if failed:
            out += ["Failed render checks: " + "; ".join(f"{n} clip {cid}: {c}" for n, cid, c in failed), ""]
    comp = r["compliance"]
    out += [f"## Compliance cases: {comp['passed']}/{comp['total']}", "", _table(
        ["case", "stage", "expected", "got", "ok"],
        [[c["case"], c["stage"], c["expected"], c["got"], c["ok"]] for c in comp["cases"]]), ""]
    jb = r["jobs"]
    out += [f"## Job queue: {jb['passed']}/{jb['total']}", "", _table(
        ["behaviour", "ok", "detail"],
        [[k, v["ok"], ", ".join(f"{a}={b}" for a, b in v.items() if a != "ok")]
         for k, v in jb.items() if isinstance(v, dict)]), ""]
    return "\n".join(out)
