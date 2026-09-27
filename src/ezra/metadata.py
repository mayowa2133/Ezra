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
and truthful to the clip; no clickbait the clip doesn't pay off. Keep the speaker's voice.
- Titles are concrete headlines, not the first spoken sentence.
- A caption is one or two short lines that add curiosity or ask the viewer something; don't repeat
  the transcript. Name the searchable subject (people, place, stakes).
- 3-5 hashtags: the creator's, one or two broad, one or two on the clip's topic. No filler tags.
- Credit the creator when one is given. Include every required hashtag and mention exactly as given."""


GENERIC = {"never", "always", "really", "think", "people", "thing", "things", "going", "because", "about",
           "would", "could", "should", "every", "everyone", "nobody", "something", "anything", "there", "their",
           "where", "which", "while", "being", "doing", "having", "maybe", "still", "those", "these", "start",
           "first", "great", "little", "honestly", "actually", "without", "before", "after", "everything",
           "middle", "literally", "gonna", "today", "video", "guys", "just", "then", "than", "what", "when",
           "with", "from", "have", "were", "will", "kill", "once", "again", "until", "made", "make", "take",
           "want", "need", "look", "come", "here", "said", "says", "even", "much", "many", "some", "only"}

# a question that invites comments, by the kind of hook; none when the hook is already a question
QUESTIONS = {"danger": "Would you have made it?", "challenge": "Who would you bet on?",
             "promise": "Who would you bet on?", "contrarian": "Agree or disagree?",
             "opinion": "Agree or disagree?", "twist": "Did you see that coming?",
             "shock": "Did you expect that?", "reveal": "What would you do first?"}
MAX_TAGS = 5


def _words(text: str) -> list[str]:
    return [t for t in tokens(text) if t.isalpha() and len(t) > 3 and t not in GENERIC]


def _topic_tags(headline: str, transcript: str, n: int = 3) -> list[str]:
    """The clip's subject: headline words that are names (capitalized past the first word) or said
    at least twice, then words the clip keeps coming back to. A one-off verb makes a useless tag, so
    fewer tags beat filler."""
    counts: dict[str, int] = {}
    for t in _words(transcript):
        counts[t] = counts.get(t, 0) + 1
    names = {w.lower() for w in re.findall(r"[A-Za-z][\w']*", headline)[1:] if w[0].isupper()}
    head = [t for t in dict.fromkeys(_words(headline)) if t in names or counts.get(t, 0) >= 2]
    head.sort(key=lambda t: -counts.get(t, 0))
    rest = [t for t, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= 3 and t not in head]
    return [f"#{t}" for t in (head + rest)[:n]]


def creator_tag(creator: str | None) -> str | None:
    tag = re.sub(r"[^a-z0-9]", "", (creator or "").lower())
    return f"#{tag}" if tag else None


def _headline(cand: Any) -> str:
    """The candidate's title when it is a written headline; its spoken hook when the title is only
    the transcript's opening words (Ezra's own windows)."""
    title = (cand.title or "").rstrip("…").strip()
    spoken = re.sub(r"\W+", " ", (cand.transcript or "").lower()).strip()
    if title and not spoken.startswith(re.sub(r"\W+", " ", title.lower()).strip()):
        return title
    return (cand.hook or title).rstrip("…").strip()


def template(clip_id: int, platforms: list[str]) -> dict[str, dict[str, Any]]:
    c = render.get_clip(clip_id)
    cand = c.candidate
    camp = campaigns.get(c.campaign_id) if c.campaign_id else None
    headline = _headline(cand)
    title = (c.title or headline)[:95]
    creator = camp.creator if camp else None
    question = None if (cand.hook_type == "question" or headline.endswith("?")) else QUESTIONS.get(cand.hook_type or "")
    topic = [*(camp.suggested_hashtags if camp else []), *_topic_tags(headline, cand.transcript)]
    tags = list(dict.fromkeys([*(camp.required_hashtags if camp else []),
                               *([creator_tag(creator)] if creator_tag(creator) else []), *topic]))
    tags = tags[:max(MAX_TAGS, len(camp.required_hashtags) if camp else 0)]
    mentions = camp.required_mentions if camp else []
    cta = camp.required_cta if camp and camp.required_cta else None
    credit = f"🎥 {creator}" if creator else None
    out: dict[str, dict[str, Any]] = {}
    for p in platforms:
        lead = f"{headline}{'' if headline[-1:] in '.!?…' else '.'} {question}" if question and headline else headline
        credits = " ".join(x for x in (credit, *mentions) if x)
        caption = "\n\n".join(x for x in (lead, cta, credits, " ".join(tags)) if x)
        entry: dict[str, Any] = {"caption": caption, "hashtags": tags}
        if p == "youtube":
            entry["title"] = title
            entry["description"] = caption
        out[p] = entry
    return out


def _schema(platforms: list[str]) -> dict[str, Any]:
    entry = {"type": "object", "required": ["caption", "hashtags", "title", "description"],
             "properties": {"caption": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
                            "title": {"type": "string"}, "description": {"type": "string"}}}
    return {"type": "object", "required": platforms, "properties": {p: entry for p in platforms}}


def generate(clip_id: int, platforms: list[str] | None = None, use_model: bool = True,
             save: bool = True, copy: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Copy for each platform: `copy` when given (an agent or person wrote it), else the model when
    configured, else the template. Required tags/mentions/CTA and the creator credit are enforced
    either way, and the publish-stage compliance result is returned."""
    c = render.get_clip(clip_id)
    camp = campaigns.get(c.campaign_id) if c.campaign_id else None
    platforms = platforms or (list(copy) if copy else None) or \
        (camp.allowed_platforms if camp else ["youtube", "tiktok", "instagram"])
    meta = template(clip_id, platforms)
    source = "template"
    if copy:
        meta = {p: {**meta[p], **copy.get(p, copy.get("default", {}))} for p in platforms}
        source = "agent"
    elif use_model and llm.get_llm().available:
        cand = c.candidate
        rules = []
        if camp:
            rules = [f"creator to credit: {camp.creator}" if camp.creator else "",
                     f"suggested hashtags: {' '.join(camp.suggested_hashtags)}" if camp.suggested_hashtags else "",
                     f"required hashtags: {' '.join(camp.required_hashtags)}" if camp.required_hashtags else "",
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
    """Required hashtags/mentions/CTA and the creator credit are guaranteed by code, and limits applied."""
    for p, m in meta.items():
        caption = m.get("caption", "")
        # a hashtag isn't a credit; the name or an @mention is
        if camp and camp.creator and not re.search(rf"(?<![#\w]){re.escape(camp.creator)}", caption, re.I):
            caption = f"{caption}\n\n🎥 {camp.creator}"
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
