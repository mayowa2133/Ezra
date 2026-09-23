"""Per-platform post metadata: title, caption, description, hashtags.

With a model configured, the ClipCritic's structured-output path writes it;
otherwise a deterministic template does. Either way the campaign's required
hashtags, mentions and CTA are enforced in code afterwards, and the publish-
stage compliance result is returned with the metadata."""

from __future__ import annotations

import re
from typing import Any

from . import campaigns, compliance, llm, render, review
from .analysis.text import tokens

LIMITS = {"youtube": {"title": 100, "caption": 5000}, "tiktok": {"caption": 2200}, "instagram": {"caption": 2200},
          "x": {"caption": 280}, "linkedin": {"caption": 3000}, "facebook": {"caption": 2200},
          "threads": {"caption": 500}}

SYSTEM = """You write short-form video post copy for a performance-paid clipping campaign. Be specific
and truthful to the clip; no clickbait the clip doesn't pay off. Keep the speaker's voice. Titles are
concrete. Captions open with the hook. Include every required hashtag and mention exactly as given."""


GENERIC = {"never", "always", "really", "think", "people", "thing", "things", "going", "because", "about",
           "would", "could", "should", "every", "everyone", "nobody", "something", "anything", "there", "their",
           "where", "which", "while", "being", "doing", "having", "maybe", "still", "those", "these", "start",
           "first", "great", "little", "honestly", "actually", "without", "before", "after"}


def _topic_tags(text: str, n: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for t in tokens(text):
        if t.isalpha() and len(t) > 4 and t not in GENERIC:
            counts[t] = counts.get(t, 0) + 1
    return [f"#{t}" for t, _ in sorted(counts.items(), key=lambda x: -x[1])[:n]]


def template(clip_id: int, platforms: list[str]) -> dict[str, dict[str, Any]]:
    c = render.get_clip(clip_id)
    cand = c.candidate
    camp = campaigns.get(c.campaign_id) if c.campaign_id else None
    hook = (cand.hook or cand.title or "").rstrip("…").strip()
    title = (c.title or cand.title or hook)[:95]
    tags = list(dict.fromkeys([*(camp.required_hashtags if camp else []), *_topic_tags(cand.transcript)]))
    mentions = camp.required_mentions if camp else []
    cta = camp.required_cta if camp and camp.required_cta else None
    excerpt = re.sub(r"\s+", " ", cand.transcript)[:220].rsplit(" ", 1)[0] + "…"
    out: dict[str, dict[str, Any]] = {}
    for p in platforms:
        parts = [hook]
        if cta:
            parts.append(cta)
        parts += [" ".join(mentions), " ".join(tags)]
        caption = "\n\n".join(x for x in parts if x)
        entry: dict[str, Any] = {"caption": caption, "hashtags": tags}
        if p == "youtube":
            entry["title"] = title
            entry["description"] = "\n\n".join(x for x in (excerpt, cta, " ".join(mentions), " ".join(tags)) if x)
        out[p] = entry
    return out


def _schema(platforms: list[str]) -> dict[str, Any]:
    entry = {"type": "object", "required": ["caption", "hashtags", "title", "description"],
             "properties": {"caption": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
                            "title": {"type": "string"}, "description": {"type": "string"}}}
    return {"type": "object", "required": platforms, "properties": {p: entry for p in platforms}}


def generate(clip_id: int, platforms: list[str] | None = None, use_model: bool = True,
             save: bool = True) -> dict[str, Any]:
    c = render.get_clip(clip_id)
    camp = campaigns.get(c.campaign_id) if c.campaign_id else None
    platforms = platforms or (camp.allowed_platforms if camp else ["youtube", "tiktok", "instagram"])
    meta = template(clip_id, platforms)
    source = "template"
    if use_model and llm.get_llm().available:
        cand = c.candidate
        rules = []
        if camp:
            rules = [f"required hashtags: {' '.join(camp.required_hashtags)}" if camp.required_hashtags else "",
                     f"required mentions: {' '.join(camp.required_mentions)}" if camp.required_mentions else "",
                     f"required CTA: {camp.required_cta}" if camp.required_cta else "",
                     f"forbidden: {', '.join(camp.forbidden_words + camp.forbidden_topics + camp.competitors)}"]
        prompt = (f"Platforms: {', '.join(platforms)}. Limits: { {p: LIMITS.get(p) for p in platforms} }.\n"
                  f"{chr(10).join(r for r in rules if r)}\nHook: {cand.hook}\nTitle idea: {cand.title}\n"
                  f"Clip transcript: {cand.transcript}")
        try:
            res = llm.call(SYSTEM, prompt, _schema(platforms), task="metadata",
                           campaign_id=c.campaign_id, source_id=c.source_id)
            meta = {p: res.data[p] for p in platforms}
            source = res.provider
        except (llm.LLMUnavailable, llm.LLMError):   # no model, or it failed: template copy
            pass
    meta = enforce(meta, camp)
    checks = {p: compliance.evaluate(camp, "publish", copy_text=_copy_text(m), platforms=[p]) for p, m in meta.items()}
    if save:
        first = meta[platforms[0]]
        review.update(clip_id, title=meta.get("youtube", first).get("title") or c.title,
                      description=meta.get("youtube", first).get("description") or first["caption"],
                      hashtags=first.get("hashtags", []), platform_metadata=meta, actor=f"metadata:{source}")
    return {"clip_id": clip_id, "source": source, "metadata": meta, "compliance": checks}


def enforce(meta: dict[str, dict[str, Any]], camp: Any | None) -> dict[str, dict[str, Any]]:
    """Required hashtags/mentions/CTA are guaranteed by code, and limits applied."""
    for p, m in meta.items():
        caption = m.get("caption", "")
        if camp:
            missing = [t for t in camp.required_hashtags if t.lower() not in caption.lower()]
            missing += [x for x in camp.required_mentions if x.lower() not in caption.lower()]
            if camp.required_cta and camp.required_cta.lower() not in caption.lower():
                caption = f"{caption}\n\n{camp.required_cta}"
            if missing:
                caption = f"{caption}\n{' '.join(missing)}"
            m["hashtags"] = list(dict.fromkeys([*camp.required_hashtags, *m.get("hashtags", [])]))
        lim = LIMITS.get(p, {})
        if "caption" in lim and len(caption) > lim["caption"]:
            caption = caption[: lim["caption"] - 1] + "…"
        m["caption"] = caption
        if m.get("title") and "title" in lim:
            m["title"] = m["title"][: lim["title"]]
    return meta


def _copy_text(m: dict[str, Any]) -> str:
    return " ".join(str(m.get(k) or "") for k in ("title", "caption", "description"))


def copy_text(clip_meta: dict[str, Any], platform: str) -> str:
    return _copy_text((clip_meta or {}).get(platform) or {})
