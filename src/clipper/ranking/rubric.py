"""The judging brief: everything the agent needs before it proposes or scores
a single clip. Weights are applied in code (ranking.scoring), never by the
agent, so a score means the same thing across runs."""

from __future__ import annotations

from typing import Any

from .. import campaigns

HOOK_TYPES = ["loss", "confession", "contrarian", "question", "how_to", "story",
              "number", "shock", "prediction", "other"]

DIMENSIONS: dict[str, str] = {
    "hook": "Do the first 1-3 seconds (the opening words as cut) create a reason to keep watching? "
            "Conflict, stakes, a bold claim or an open loop score high; throat-clearing scores low.",
    "retention": "Does tension hold to the end, with a payoff late rather than early? No dead air, "
                 "no tangent in the middle.",
    "context": "Does it make sense to someone who never saw the episode? Penalise unresolved "
               "pronouns ('he', 'that thing') and references to earlier discussion.",
    "emotion": "Intensity of feeling: surprise, anger, vulnerability, humour, awe.",
    "novelty": "Would a viewer in this niche hear something they have not heard a hundred times?",
    "comment": "Would people argue, share their own story, or tag someone?",
    "campaign_fit": "Fits the campaign brief, audience and rules (including judgment-only rules "
                    "such as 'creator visible').",
}


def brief(campaign_ref: str | int, n_candidates: int = 20) -> dict[str, Any]:
    from ..analytics import insights  # analytics imports ranking; keep this lazy

    spec = campaigns.spec(campaign_ref)
    camp = campaigns.get(campaign_ref)
    learned = insights.for_brief()
    return {
        "campaign": {
            "slug": camp["slug"], "name": spec.name, "platform": spec.platform,
            "cpm": spec.rate.cpm, "minimum_views": spec.minimum_views,
            "maximum_payout": spec.maximum_payout, "brief": spec.brief,
            "duration_seconds": {"min": spec.requirements.min_duration, "max": spec.requirements.max_duration},
            "forbidden": spec.forbidden, "forbidden_terms": spec.forbidden_terms,
            "competitors": spec.competitors, "required": spec.required,
            "hashtags": spec.hashtags, "platforms": spec.platforms,
        },
        "rubric": {
            "scale": "Score every dimension 0-100. 50 = an average clip a competent editor would post; "
                     "80+ = you would bet on it; 90+ is rare. Use the whole range.",
            "dimensions": {k: {"weight": round(spec.weights[k], 3), "question": q} for k, q in DIMENSIONS.items()},
            "weights_applied_by": "clipper (score each dimension honestly; do not pre-weight)",
        },
        "hook_types": HOOK_TYPES,
        "finding_instructions": [
            f"Propose about {n_candidates} candidate moments per source, across the whole episode.",
            f"Each must be {spec.requirements.min_duration:g}-{spec.requirements.max_duration:g}s long. "
            "clipper snaps your start/end to word boundaries, so rough seconds are fine.",
            "Start ON the hook: cut the preamble so the first sentence carries the tension.",
            "End just after the payoff; do not trail into the next topic.",
            "Every clip must stand alone without the rest of the episode.",
            "Skip intros, outros, ad reads and anything that breaks a forbidden rule.",
        ],
        "learned_from_past_performance": learned,
    }
