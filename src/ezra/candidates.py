"""Candidate generation and ranking.

    source → transcript → topic segments → candidate windows (ClipScout)
           → cheap features + heuristic factor scores
           → diversity-aware shortlist (DiversityCritic)
           → compliance (ComplianceCritic, independent of scores)
           → optional model critique of the shortlist (ClipCritic)
           → performance prior from history (PerformanceCritic)
           → rank score + expected value → render only the strongest

Nothing is rendered here; renders cost minutes, candidates cost milliseconds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, select

from . import analysis, campaigns, compliance, db, economics, llm, sources
from . import analytics as perf
from .config import get_settings
from .db.models import Campaign, Candidate, Clip
from .scoring import heuristic
from .scoring.features import Window, ad_regions, extract, sponsor_terms
from .transcription import edges, load_segments, snap
from .transcription.base import Segment

Progress = Callable[[float, str], None]
FACTORS = ["hook", "retention", "context", "emotion", "novelty", "discussion", "payoff", "visual", "campaign_fit"]
FACTOR_COLUMNS = {"hook": "hook_score", "retention": "retention_score", "context": "context_score",
                  "emotion": "emotion_score", "novelty": "novelty_score", "discussion": "discussion_score",
                  "payoff": "payoff_score", "visual": "visual_score", "campaign_fit": "campaign_fit_score"}


# --- ClipScout ------------------------------------------------------------------

def scout_windows(source_id: int, segments: list[Segment], topics: list[dict[str, Any]],
                  min_d: float, max_d: float) -> list[Window]:
    """Every sentence start, at three target lengths, ending on a sentence end
    inside [min_d, max_d]."""
    targets = sorted({round(min_d + (max_d - min_d) * f, 1) for f in (0.15, 0.4, 0.75)})
    host = _questioner(segments)
    out: list[Window] = []
    seen: set[tuple[int, int]] = set()
    for i in range(len(segments)):
        if i > 0 and not segments[i - 1].sentence_end:
            continue            # a segment that continues a sentence (split at a pause or music hit)
        for target in targets:
            j = i
            while j < len(segments) and segments[j].end - segments[i].start < target:
                j += 1
            if j >= len(segments):
                j = len(segments) - 1
            # walk back to a sentence end still >= min_d, else forward
            k = j
            while k > i and not segments[k].sentence_end:
                k -= 1
            if segments[k].end - segments[i].start < min_d:
                k = j
                while k < len(segments) - 1 and not segments[k].sentence_end:
                    k += 1
            # don't end on a question that opens the next topic
            while k > i and segments[k].text.rstrip().endswith("?") and \
                    segments[k - 1].sentence_end and segments[k - 1].end - segments[i].start >= min_d:
                k -= 1
            # nor on the interviewer's short segue into the next subject
            # ("Let us do a quick lightning round."): end on the answer before it
            if host is not None:
                t = k
                while t > i and segments[t].speaker == host and segments[t - 1].speaker == host:
                    t -= 1
                tail = segments[t:k + 1]
                if (t > i and segments[t].speaker == host and segments[t - 1].speaker != host
                        and segments[k].end - segments[t].start < 6.0
                        and segments[t - 1].sentence_end and segments[t - 1].end - segments[i].start >= min_d
                        and all(x.speaker == host for x in tail)):
                    k = t - 1
            dur = segments[k].end - segments[i].start
            if not (min_d - 0.5 <= dur <= max_d + 0.5) or (i, k) in seen:
                continue
            seen.add((i, k))
            topic = next((t for t in topics if t["start"] <= segments[i].start < t["end"]), None)
            out.append(Window(source_id, segments[i].start, segments[k].end, segments[i:k + 1],
                              before=segments[max(0, i - 2):i], after=segments[k + 1:k + 3], topic=topic))
    return out


def _questioner(segments: list[Segment]) -> str | None:
    """The speaker who asks most of the questions (the interviewer), if the
    source is a conversation with a clear one."""
    asked: dict[str, int] = {}
    for sg in segments:
        if sg.speaker and sg.text.rstrip().endswith("?"):
            asked[sg.speaker] = asked.get(sg.speaker, 0) + 1
    speakers = {sg.speaker for sg in segments if sg.speaker}
    if len(speakers) < 2 or not asked:
        return None
    top, n = max(asked.items(), key=lambda kv: kv[1])
    return top if n >= 3 and n >= 2 * (sum(asked.values()) - n) else None


def _containment(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = min(a[1] - a[0], b[1] - b[0])
    return inter / shorter if shorter > 0 else 0.0


def _iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def diverse_shortlist(items: list[dict[str, Any]], limit: int, max_iou: float = 0.45,
                      max_per_topic: int = 4) -> list[dict[str, Any]]:
    """Greedy non-maximum suppression by content score, capped per topic."""
    chosen: list[dict[str, Any]] = []
    per_topic: dict[str, int] = {}
    for it in sorted(items, key=lambda x: -x["content"]):
        span = (it["window"].start, it["window"].end)
        if any(_iou(span, (c["window"].start, c["window"].end)) > max_iou for c in chosen):
            continue
        tkey = (it["window"].topic or {}).get("label", "?")
        if per_topic.get(tkey, 0) >= max_per_topic:
            continue
        per_topic[tkey] = per_topic.get(tkey, 0) + 1
        chosen.append(it)
        if len(chosen) >= limit:
            break
    return chosen


def _hook_text(first: str) -> str:
    text = re.sub(r"^\s*(so|and|but|um|uh|well|okay|anyway)[,\s]+", "", first, flags=re.I).strip()
    words = text.split()
    return " ".join(words[:9]).rstrip(",;:") + ("…" if len(words) > 9 else "")


def _campaign_dict(c: Campaign | None) -> dict[str, Any] | None:
    return campaigns.to_dict(c) if c else None


def _campaign_for(src: Any, campaign: str | int | None) -> Any:
    """The campaign named explicitly, else the source's own, else None."""
    ref = campaign if campaign is not None else src.campaign_id
    return campaigns.get(ref) if ref is not None else None


def find_candidates(source_id: int, campaign: str | int | None = None, max_candidates: int = 40,
                    progress: Progress | None = None) -> list[Candidate]:
    say = progress or (lambda f, m: None)
    src = sources.get(source_id)
    camp = _campaign_for(src, campaign)
    if src.status != "analyzed" or analysis.get(source_id, "topics") is None:
        analysis.analyze_source(source_id, progress=lambda f, m: say(0.6 * f, m))
    say(0.62, "proposing windows")
    segments = load_segments(source_id)
    rows = analysis.all_for(source_id)
    topics = rows["topics"].data["topics"] if "topics" in rows else []
    scenes = rows["scenes"].data.get("scenes", []) if "scenes" in rows else []
    activity = rows["scenes"].data.get("activity_per_second", []) if "scenes" in rows else []
    silence = rows["silence"].data.get("regions", []) if "silence" in rows else []
    faces = rows["faces"].data.get("timeline") if "faces" in rows else None
    loud = rows["loudness"].data if "loudness" in rows else None
    min_d, max_d = (camp.min_duration, camp.max_duration) if camp else (15.0, 60.0)
    windows = scout_windows(source_id, segments, topics, min_d, max_d)
    ads = ad_regions(segments)
    brands = sponsor_terms(segments, ads)
    from .analysis.text import tokens

    df: dict[str, int] = {}
    for seg in segments:
        for t in set(tokens(seg.text)):
            df[t] = df.get(t, 0) + 1
    weights = campaigns.normalized_weights(camp.weights if camp else None)
    cdict = _campaign_dict(camp)
    items = []
    for w in windows:
        f = extract(w, silence, activity, faces, scenes, topics, df, len(segments), loudness=loud)
        in_ad = sum(max(0.0, min(w.end, b) - max(w.start, a)) for a, b in ads) / max(1e-6, w.end - w.start)
        if in_ad > 0.3:          # mostly inside a sponsor segment, even if the window's own words are few
            f["ad_read"] = max(int(f.get("ad_read", 0)), 2)
        low = w.text.lower()
        f["ad_read"] = int(f.get("ad_read", 0)) + sum(low.count(b) for b in brands)
        scores, why = heuristic.score(f, cdict)
        items.append({"window": w, "features": f, "scores": scores, "why": why,
                      "content": heuristic.content_score(scores, weights)})
    say(0.75, f"scored {len(items)} windows")
    shortlist = diverse_shortlist(items, max_candidates)
    words = [w for s in segments for w in s.words]
    with db.session() as s:
        # re-running the scout replaces its own unrendered candidates
        rendered = select(Clip.candidate_id)
        s.execute(delete(Candidate).where(Candidate.source_id == source_id, Candidate.origin == "scout",
                                          Candidate.id.not_in(rendered)))
    out_ids = []
    for it in shortlist:
        w, f = it["window"], it["features"]
        start, end = snap(words, w.start, w.end, src.duration)
        comp = compliance.evaluate(camp, "candidate", duration=end - start, transcript=w.text, source=src,
                                   speakers=f["speakers"])
        first = w.segments[0].text
        with db.session() as s:
            cand = Candidate(
                campaign_id=camp.id if camp else None, source_id=source_id, start=start, end=end, transcript=w.text,
                speakers=f["speakers"], topic=(w.topic or {}).get("label"), hook=_hook_text(first),
                hook_type=heuristic.hook_type(first, f), title=_hook_text(first).rstrip("…"),
                reason="; ".join(f"{k}: {v}" for k, v in it["why"].items() if k in ("hook", "payoff", "context")),
                context_before=" ".join(x.text for x in w.before), context_after=" ".join(x.text for x in w.after),
                origin="scout", features={k: v for k, v in f.items() if k != "vocab"}, scorer="heuristic",
                confidence=heuristic.CONFIDENCE, content_score=it["content"],
                score_explanations={"heuristic": it["why"], "heuristic_scores": it["scores"]},
                compliance_status=comp["status"], compliance_reasons=comp["reasons"],
                **{FACTOR_COLUMNS[k]: v for k, v in it["scores"].items()})
            s.add(cand)
            s.flush()
            out_ids.append(cand.id)
    say(0.85, f"{len(out_ids)} candidates")
    if get_settings().clip_engine == "openshorts":
        from . import openshorts

        say(0.86, "asking OpenShorts for its moments")
        extra = openshorts.run(source_id, campaign if campaign is not None else src.campaign_id,
                               on_log=lambda m: say(0.86, f"openshorts: {m}"))
        out_ids += [c.id for c in extra]
    rank(source_id=source_id, progress=lambda f, m: say(0.85 + 0.15 * f, m))
    return [get(i) for i in out_ids]


def _evaluate_span(source_id: int, camp: Campaign | None, start: float, end: float) -> dict[str, Any]:
    """Snap [start, end] to words and score it like any scouted window (features,
    heuristic factors, ad-segment check, candidate-stage compliance)."""
    src = sources.get(source_id)
    segments = load_segments(source_id)
    words = [w for s in segments for w in s.words]
    s0, e0 = snap(words, start, end, src.duration)
    inside = [seg for seg in segments if seg.end > s0 and seg.start < e0]
    if not inside:
        raise ValueError(f"no speech between {start:.1f}s and {end:.1f}s")
    win = Window(source_id, s0, e0, inside)
    rows = analysis.all_for(source_id)
    f = extract(win, rows["silence"].data.get("regions", []) if "silence" in rows else [],
                rows["scenes"].data.get("activity_per_second", []) if "scenes" in rows else [],
                rows["faces"].data.get("timeline") if "faces" in rows else None,
                rows["scenes"].data.get("scenes", []) if "scenes" in rows else [],
                rows["topics"].data["topics"] if "topics" in rows else [], {}, 1,
                loudness=rows["loudness"].data if "loudness" in rows else None)
    ads = ad_regions(segments)
    in_ad = sum(max(0.0, min(e0, b) - max(s0, a)) for a, b in ads) / max(1e-6, e0 - s0)
    if in_ad > 0.3:
        f["ad_read"] = max(int(f.get("ad_read", 0)), 2)
    low = win.text.lower()
    f["ad_read"] = int(f.get("ad_read", 0)) + sum(low.count(b) for b in sponsor_terms(segments, ads))
    scores, why = heuristic.score(f, _campaign_dict(camp))
    weights = campaigns.normalized_weights(camp.weights if camp else None)
    comp = compliance.evaluate(camp, "candidate", duration=e0 - s0, transcript=win.text, source=src,
                               speakers=f["speakers"])
    return {"start": s0, "end": e0, "inside": inside, "win": win, "f": f, "scores": scores, "why": why,
            "content": heuristic.content_score(scores, weights), "comp": comp}


def create_custom(source_id: int, start: float, end: float, title: str | None = None, hook: str | None = None,
                  hook_type: str | None = None, reason: str | None = None, origin: str = "manual",
                  campaign: str | int | None = None) -> Candidate:
    """A candidate chosen by a person or an agent. Boundaries snap to words."""
    src = sources.get(source_id)
    camp = _campaign_for(src, campaign)
    ev = _evaluate_span(source_id, camp, start, end)
    s0, e0, inside, win, f, scores, why, comp = (ev["start"], ev["end"], ev["inside"], ev["win"], ev["f"],
                                                 ev["scores"], ev["why"], ev["comp"])
    with db.session() as s:
        cand = Candidate(campaign_id=camp.id if camp else None, source_id=source_id, start=s0, end=e0,
                         transcript=win.text, speakers=f["speakers"], title=title or _hook_text(inside[0].text),
                         hook=hook or _hook_text(inside[0].text),
                         hook_type=hook_type or heuristic.hook_type(inside[0].text, f), reason=reason, origin=origin,
                         features={k: v for k, v in f.items() if k != "vocab"}, scorer="heuristic",
                         confidence=heuristic.CONFIDENCE, content_score=ev["content"],
                         score_explanations={"heuristic": why, "heuristic_scores": scores},
                         compliance_status=comp["status"], compliance_reasons=comp["reasons"],
                         **{FACTOR_COLUMNS[k]: v for k, v in scores.items()})
        s.add(cand)
        s.flush()
        cid = cand.id
    rank(candidate_ids=[cid])
    return get(cid)


# --- ranking ----------------------------------------------------------------------

CRITIC_SYSTEM = """You are ClipCritic, a senior short-form editor choosing moments from long-form video
(podcasts, interviews, challenge and competition videos, vlogs, streams) for TikTok, Instagram Reels and
YouTube Shorts, on behalf of a campaign that is paid per qualified view.

What high-performing clips share, and what you reward:
- The first sentence states the premise or lands a reaction. Top Shorts open like "How many people does
  it take to stop Ronaldo?", "For $10,000, will you go to the North Pole?", "Sticky versus slippery
  stairs.", "Bear! Bear! Bear!". Openings that lean on earlier context ("You know, we...", "But what
  the cops...", "And we got intel...", "The reason I've been...") score low on hook and context: trim
  to the premise line when one exists.
- One self-contained story: setup, escalation, payoff. The clip ends on the resolution (the reveal,
  the win, the arrest, the reaction, the punchline), not mid-action ("Hold on, hold on...") or on a
  transition. Extend into the later sentences to reach it when needed.
- Leave room for the action: reactions, crashes and cheering between lines are part of the clip.
- Top clips of this kind run about 20-50 seconds (median ~35 s); don't cut below 20 s unless the story
  is complete.
- Clear stakes a stranger understands without the episode (money, elimination, danger, a secret).
- No sponsor reads or product plugs (programmes don't pay for ads): score them very low.

Score each candidate on every factor 0-100 (50 = an average clip a competent editor would post,
80+ = you would bet on it, 90+ is rare). Compare candidates against each other and use the full range.
Each candidate is given as numbered sentences. In `keep`, choose the sentence range that makes the best
clip (open on the strongest line, end on the payoff) and score the clip *as kept*.
`hook_text` is the on-screen hook card: at most 8 punchy words that make a stranger stop scrolling
(e.g. "He paid 100 cops to catch him"), not a quote of the first line. `title` is a specific
YouTube-style title. `reason`: at most 40 words, concrete: what the opening does, whether it stands
alone, where the payoff lands. Scores are ranking estimates, not predictions. For each listed review rule, say
whether the clip complies."""


def _critic_schema(rule_ids: list[int]) -> dict[str, Any]:
    factor_props = {k: {"type": "number", "minimum": 0, "maximum": 100} for k in FACTORS}
    return {"type": "object", "required": ["candidates"], "properties": {"candidates": {"type": "array", "items": {
        "type": "object", "required": ["id", "scores", "hook_text", "title", "hook_type", "reason", "keep",
                                       "rule_checks"],
        "properties": {
            "id": {"type": "integer"},
            "scores": {"type": "object", "required": FACTORS, "properties": factor_props},
            "hook_text": {"type": "string", "description": "On-screen hook, max 10 words"},
            "title": {"type": "string", "description": "Specific YouTube-style title, max 90 chars"},
            "hook_type": {"type": "string", "enum": heuristic.HOOK_TYPES},
            "reason": {"type": "string", "description": "At most 40 words"},
            "keep": {"type": "object", "required": ["from", "to"],
                     "description": "Sentence numbers to keep, inclusive. Tighten to open on the strongest "
                                    "line and end on the payoff; you may extend into the listed later "
                                    "sentences to reach it. Keep all sentences if the cut is already right.",
                     "properties": {"from": {"type": "integer"}, "to": {"type": "integer"}}},
            "rule_checks": {"type": "array", "items": {"type": "object", "required": ["rule_id", "compliant", "reason"],
                                                        "properties": {"rule_id": {"type": "integer"},
                                                                       "compliant": {"type": "boolean"},
                                                                       "reason": {"type": "string"}}}}}}}}}


_SEGMENT_CACHE: dict[int, list[Any]] = {}


def _segments(source_id: int) -> list[Any]:
    if source_id not in _SEGMENT_CACHE:
        try:
            _SEGMENT_CACHE[source_id] = load_segments(source_id)
        except RuntimeError:          # no transcript: the critic sees no sentences, keeps the cut
            _SEGMENT_CACHE[source_id] = []
    return _SEGMENT_CACHE[source_id]


CRITIC_BATCH = 6        # candidates per model call (a CLI structured call takes ~30 s per candidate)
CRITIC_PARALLEL = 5     # all batches of a 25-candidate shortlist at once


def critic_pass(cands: list[Candidate], camp: Campaign | None) -> dict[int, dict[str, Any]]:
    """Model critique of the shortlist in parallel batches; {} when no model is configured.
    A batch that fails (timeout, bad output) is skipped: those candidates keep heuristic scores."""
    provider = llm.get_llm()
    if not provider.available or not cands:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    batches = [cands[i:i + CRITIC_BATCH] for i in range(0, len(cands), CRITIC_BATCH)]
    learned = [x.text for x in perf.learnings()]          # read once, not from every thread
    _SEGMENT_CACHE.clear()                                   # fresh per pass (transcripts can be redone)
    for sid in {c.source_id for c in cands}:
        _segments(sid)
    out: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=CRITIC_PARALLEL) as pool:
        for part in pool.map(lambda b: _critic_batch(b, camp, learned), batches):
            out.update(part)
    return out


def _critic_batch(cands: list[Candidate], camp: Campaign | None, learned: list[str]) -> dict[int, dict[str, Any]]:
    review_rules = [r for r in (camp.rules if camp else []) if r.kind in ("forbidden_topic", "freeform")]
    lines = []
    if camp:
        lines.append(f"Campaign: {camp.name}. Brief: {camp.brief or '-'}. Duration {camp.min_duration:g}-"
                     f"{camp.max_duration:g}s. Platforms: {', '.join(camp.allowed_platforms)}.")
        if review_rules:
            lines.append("Review rules:\n" + "\n".join(f"  rule {r.id}: {r.description}" for r in review_rules))
    if learned:
        lines.append("Learned from past performance:\n" + "\n".join(f"  - {t}" for t in learned[:10]))
    sentences: dict[int, list[Any]] = {}
    for c in cands:
        segs = _segments(c.source_id)
        inside = [sg for sg in segs if sg.end > c.start + 0.05 and sg.start < c.end - 0.05]
        after = [sg for sg in segs if sg.start >= c.end - 0.05][:3]
        sentences[c.id] = inside + after
        numbered = "\n".join(f"  {i + 1}. [{sg.start:.1f}s] {sg.text}" + ("   (after the clip)" if sg in after else "")
                             for i, sg in enumerate(inside + after))
        lines.append(f"\n[id {c.id}] {c.start:.1f}-{c.end:.1f}s ({c.end - c.start:.0f}s)"
                     f"\nContext before: {(c.context_before or '')[-300:]}\nSentences:\n{numbered}")
    try:
        res = llm.call(CRITIC_SYSTEM, "\n".join(lines), _critic_schema([r.id for r in review_rules]),
                       task="clip_critic", campaign_id=camp.id if camp else None, source_id=cands[0].source_id)
    except llm.LLMUnavailable:
        return {}
    except llm.LLMError as e:
        import logging

        logging.getLogger("ezra.critic").warning("critic batch skipped (%s); heuristic scores kept", e)
        return {}
    wanted = {c.id for c in cands}
    lo, hi = (camp.min_duration, camp.max_duration) if camp else (15.0, 60.0)
    out: dict[int, dict[str, Any]] = {}
    for x in res.data.get("candidates", []):
        cid = int(x["id"])
        if cid not in wanted:
            continue
        item = x | {"_provider": res.provider}
        keep, sents = x.get("keep") or {}, sentences.get(cid, [])
        i, j = int(keep.get("from", 0)) - 1, int(keep.get("to", 0)) - 1
        if 0 <= i <= j < len(sents) and lo - 0.5 <= sents[j].end - sents[i].start <= hi + 0.5:
            item["_bounds"] = (sents[i].start, sents[j].end)
        out[cid] = item
    return out


def _retrim(cand: Candidate, camp: Campaign | None, start: float, end: float) -> Candidate:
    """Move a candidate to new bounds (the critic's tighter cut), re-scored and re-checked."""
    ev = _evaluate_span(cand.source_id, camp, start, end)
    with db.session() as s:
        row = s.get(Candidate, cand.id)
        assert row is not None
        explanations = dict(row.score_explanations or {})
        explanations.update(heuristic=ev["why"], heuristic_scores=ev["scores"],
                            critic_trim={"from": [round(row.start, 2), round(row.end, 2)],
                                         "to": [ev["start"], ev["end"]]})
        row.start, row.end, row.transcript = ev["start"], ev["end"], ev["win"].text
        row.features = {k: v for k, v in ev["f"].items() if k != "vocab"}
        row.speakers, row.content_score, row.score_explanations = ev["f"]["speakers"], ev["content"], explanations
        row.compliance_status, row.compliance_reasons = ev["comp"]["status"], ev["comp"]["reasons"]
        for k, v in ev["scores"].items():
            setattr(row, FACTOR_COLUMNS[k], v)
    return get(cand.id)


def rank(source_id: int | None = None, campaign: str | int | None = None, candidate_ids: list[int] | None = None,
         use_model: bool = True, critic_top: int = 25, progress: Progress | None = None) -> list[Candidate]:
    say = progress or (lambda f, m: None)
    with db.session() as s:
        q = select(Candidate).where(Candidate.status.in_(("new", "ranked")))
        if source_id is not None:
            q = q.where(Candidate.source_id == source_id)
        if campaign is not None:
            q = q.where(Candidate.campaign_id == campaigns.get(campaign).id)
        if candidate_ids:
            q = q.where(Candidate.id.in_(candidate_ids))
        cands = list(s.scalars(q))
    if not cands:
        return []
    camp_ids = {c.campaign_id for c in cands}
    camps = {cid: campaigns.get(cid) for cid in camp_ids if cid is not None}
    models = {cid: economics.view_model(cid) for cid in camps}
    critique: dict[int, dict[str, Any]] = {}
    if use_model:
        for cid, camp in [(None, None)] + list(camps.items()):
            group = sorted([c for c in cands if c.campaign_id == cid], key=lambda c: -(c.content_score or 0))
            group = [c for c in group if c.compliance_status != "FAIL"][:critic_top]
            if group:
                say(0.2, f"model critique of {len(group)} candidates")
                critique.update(critic_pass(group, camp))
    say(0.7, "ranking")
    # pass 1: each candidate's own score (agent > critic blend > heuristic, prior, ad penalty)
    scored: list[dict[str, Any]] = []
    for c in cands:
        camp = camps.get(c.campaign_id) if c.campaign_id else None
        bounds = (critique.get(c.id) or {}).get("_bounds")
        if bounds and (abs(bounds[0] - c.start) > 0.4 or abs(bounds[1] - c.end) > 0.4):
            try:
                c = _retrim(c, camp, *bounds)
            except ValueError:
                pass
        weights = campaigns.normalized_weights(camp.weights if camp else None)
        heur = (c.score_explanations or {}).get("heuristic_scores") or {k: getattr(c, FACTOR_COLUMNS[k]) or 50
                                                                         for k in FACTORS}
        agent = (c.score_explanations or {}).get("agent_scores")
        crit = critique.get(c.id)
        explanations = dict(c.score_explanations or {})
        if agent:  # scores supplied by an MCP agent win over the heuristic
            final = {k: float(agent[k]) for k in FACTORS}
            scorer, conf = "agent+heuristic", 0.65
        elif crit:
            final = {k: round(0.65 * float(crit["scores"][k]) + 0.35 * float(heur[k]), 1) for k in FACTORS}
            scorer, conf = f"{crit['_provider']}+heuristic", 0.6
            explanations["critic"] = {"reason": crit["reason"], "scores": crit["scores"]}
        else:
            final = {k: float(heur[k]) for k in FACTORS}
            scorer, conf = "heuristic", heuristic.CONFIDENCE
        content = heuristic.content_score(final, weights)
        feat = dict(c.features or {})
        feat.update(hook_type=(crit or {}).get("hook_type") or c.hook_type, topic=c.topic,
                    opening=" ".join(re.findall(r"[a-z0-9$']+", (c.transcript or "").lower())[:2]))
        prior_score, prior_conf, basis = perf.prior(feat, c.campaign_id)
        alpha = 0.35 * prior_conf
        ad = int((c.features or {}).get("ad_read", 0)) >= 2
        explanations["performance_prior"] = {"score": prior_score, "confidence": prior_conf, "basis": basis}
        if ad:
            explanations["ad_read"] = "sponsor/ad read: programmes pay for the creator's content, not ads"
        scored.append({"c": c, "camp": camp, "crit": crit, "final": final, "scorer": scorer, "conf": conf,
                       "content": content, "prior": prior_score, "explanations": explanations,
                       "base": (1 - alpha) * content + alpha * prior_score - (30.0 if ad else 0.0),
                       "ad_penalty": 30.0 if ad else 0.0})

    # pass 2: in final-score order, a later cut of an already-kept story is a duplicate. Overlap is
    # measured against the shorter clip, so a 16 s cut inside a 34 s one counts even at low IoU.
    kept: list[tuple[float, float]] = []
    for item in sorted(scored, key=lambda x: -x["base"]):
        c, camp, crit, final = item["c"], item["camp"], item["crit"], item["final"]
        scorer, conf, content, prior_score = item["scorer"], item["conf"], item["content"], item["prior"]
        explanations = item["explanations"]
        span = (c.start, c.end)
        dup = any(_containment(span, k) > 0.5 for k in kept)
        penalty = item["ad_penalty"] + (15.0 if dup else 0.0)
        if c.compliance_status != "FAIL" and not dup:
            kept.append(span)
        rank_score = round(item["base"] - (15.0 if dup else 0.0), 2)
        if dup:
            explanations["diversity"] = "overlaps a higher-ranked candidate (same story)"
        comp: dict[str, Any] = {"status": c.compliance_status, "reasons": c.compliance_reasons}
        if crit and crit.get("rule_checks"):
            verdicts = {str(r["rule_id"]): r for r in crit["rule_checks"]}
            src = sources.get(c.source_id)
            comp = compliance.evaluate(camp, "candidate", duration=c.end - c.start, transcript=c.transcript,
                                       source=src, speakers=c.speakers, llm_rule_verdicts=verdicts)
        ev = economics.expected_value(camp, rank_score, models.get(camp.id)) if camp else {}
        with db.session() as s:
            row = s.get(Candidate, c.id)
            assert row is not None
            for k, v in final.items():
                setattr(row, FACTOR_COLUMNS[k], v)
            row.content_score = content
            row.performance_prior = prior_score
            row.diversity_penalty = penalty
            row.rank_score = rank_score
            row.scorer = scorer
            row.confidence = conf
            row.score_explanations = explanations
            row.compliance_status = comp["status"]
            row.compliance_reasons = comp["reasons"]
            row.expected_value = ev
            if crit:
                row.hook = crit["hook_text"]
                row.title = crit["title"]
                row.hook_type = crit["hook_type"]
                row.reason = crit["reason"]
            if row.status == "new":
                row.status = "ranked"
    say(1.0, "ranked")
    return [get(c.id) for c in cands]


def set_agent_scores(candidate_id: int, scores: dict[str, float], notes: str | None = None,
                     hook: str | None = None, title: str | None = None, hook_type: str | None = None,
                     compliant: bool = True, compliance_notes: str | None = None) -> Candidate:
    """Scores from an MCP agent. Stored per factor; ClipScout/ranking then apply
    campaign weights, the performance prior and the diversity penalty."""
    missing = [k for k in FACTORS if k not in scores]
    if missing:
        raise ValueError(f"missing factor scores: {missing}")
    for k in FACTORS:
        if not 0 <= float(scores[k]) <= 100:
            raise ValueError(f"{k} must be 0-100")
    if hook_type and hook_type not in heuristic.HOOK_TYPES:
        raise ValueError(f"hook_type must be one of {heuristic.HOOK_TYPES}")
    with db.session() as s:
        c = s.get(Candidate, candidate_id)
        if c is None:
            raise LookupError(f"no candidate {candidate_id}")
        ex = dict(c.score_explanations or {})
        ex["agent_scores"] = {k: float(scores[k]) for k in FACTORS}
        if notes:
            ex["agent_notes"] = notes
        c.score_explanations = ex
        if hook:
            c.hook = hook
        if title:
            c.title = title
        if hook_type:
            c.hook_type = hook_type
        if notes:
            c.reason = notes
        if not compliant:
            reasons = list(c.compliance_reasons or [])
            reasons.append({"rule_id": None, "kind": "agent", "outcome": "review",
                            "message": compliance_notes or "agent flagged a compliance concern", "stage": "candidate"})
            c.compliance_reasons = reasons
            if c.compliance_status == "PASS":
                c.compliance_status = "REVIEW_REQUIRED"
        if c.status == "discarded":
            c.status = "ranked"
    rank(candidate_ids=[candidate_id], use_model=False)
    return get(candidate_id)


def get(candidate_id: int) -> Candidate:
    with db.session() as s:
        c = s.get(Candidate, candidate_id)
        if c is None:
            raise LookupError(f"no candidate {candidate_id}")
        return c


def list_candidates(campaign: str | int | None = None, source_id: int | None = None, top: int | None = None,
                    include_failed: bool = False, status: str | None = None) -> list[Candidate]:
    with db.session() as s:
        q = select(Candidate)
        if campaign is not None:
            q = q.where(Candidate.campaign_id == campaigns.get(campaign).id)
        if source_id is not None:
            q = q.where(Candidate.source_id == source_id)
        if status:
            q = q.where(Candidate.status == status)
        items = list(s.scalars(q))
    publishable = [c for c in items if c.compliance_status != "FAIL"]
    failed = [c for c in items if c.compliance_status == "FAIL"]
    key = lambda c: -(c.rank_score if c.rank_score is not None else (c.content_score or 0))  # noqa: E731
    ordered = sorted(publishable, key=key) + (sorted(failed, key=key) if include_failed else [])
    return ordered[:top] if top else ordered


def discard(candidate_id: int) -> None:
    with db.session() as s:
        c = s.get(Candidate, candidate_id)
        if c is None:
            raise LookupError(f"no candidate {candidate_id}")
        c.status = "discarded"


def to_dict(c: Candidate, detail: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": c.id, "campaign_id": c.campaign_id, "source_id": c.source_id, "start": c.start, "end": c.end,
        "duration": round(c.end - c.start, 1), "title": c.title, "hook": c.hook, "hook_type": c.hook_type,
        "topic": c.topic, "origin": c.origin, "status": c.status, "rank_score": c.rank_score,
        "content_score": c.content_score, "confidence": c.confidence, "scorer": c.scorer,
        "scores": {k: getattr(c, FACTOR_COLUMNS[k]) for k in FACTORS},
        "performance_prior": c.performance_prior, "diversity_penalty": c.diversity_penalty,
        "compliance": {"status": c.compliance_status, "reasons": c.compliance_reasons},
        "expected_value": c.expected_value, "speakers": c.speakers, "reason": c.reason,
    }
    if detail:
        d.update(transcript=c.transcript, context_before=c.context_before, context_after=c.context_after,
                 explanations=c.score_explanations, features=c.features)
        try:
            words = [w for seg in load_segments(c.source_id) for w in seg.words if w.e > c.start and w.s < c.end]
            d.update(edges(words))
        except RuntimeError:
            pass
    return d
