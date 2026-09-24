"""Candidate windows and heuristic scoring rules on hand-built transcripts."""

from ezra.candidates import _questioner, scout_windows
from ezra.scoring import heuristic
from ezra.transcription.base import Segment


def seg(start: float, end: float, text: str, spk: str) -> Segment:
    return Segment(start, end, text, speaker=spk)


INTERVIEW = [
    seg(0, 3, "What is the biggest mistake you made?", "S1"),
    seg(3.5, 9, "I raised two million dollars before we had one paying customer.", "S2"),
    seg(9.5, 15, "It made us lazy and we hired far too fast for the revenue we had.", "S2"),
    seg(15.5, 20, "That cost us the company in the end, and I would never do it again.", "S2"),
    seg(20.5, 22.5, "Let us do a quick lightning round.", "S1"),
    seg(23, 24, "Morning routine?", "S1"),
    seg(24.5, 27, "Coffee and a walk, and no email until ten.", "S2"),
    seg(27.5, 29, "Best book this year?", "S1"),
    seg(29.5, 33, "A history of shipping containers, surprisingly exciting.", "S2"),
    seg(33.5, 35, "Last question for you?", "S1"),
]


def test_questioner_is_the_interviewer():
    assert _questioner(INTERVIEW) == "S1"
    assert _questioner([seg(0, 2, "Why?", "S1"), seg(2, 4, "Because.", "S2")]) is None   # too few questions


def test_windows_do_not_end_on_the_interviewers_segue():
    wins = scout_windows(1, INTERVIEW, [], 15, 60)
    from_start = [w for w in wins if w.start == 0]
    assert from_start
    for w in from_start:
        last = w.segments[-1]
        assert not (last.speaker == "S1" and not last.text.endswith("?")), last.text
    assert any(w.end == 20 for w in from_start)          # ends on the answer's last line


def _features(**over):
    f = {"first_lexicon": {}, "first_has_number": False, "first_person_claim": True, "first_is_question": False,
         "first_sentence_words": 10, "starts_with_connector": False, "starts_with_filler": False,
         "starts_with_dangling": False, "ends_sentence": True, "payoff_shift": 0.0, "dead_air_ratio": 0.0,
         "wpm": 170, "topics_spanned": 1, "duration": 30, "backrefs": 0, "intensity": 0.3, "laughter": 0,
         "exclamations": 0, "lexicon": {}, "contrarian": 0, "rarity": 1.0, "opinion": 0, "questions": 0,
         "you_address": 0, "last_has_lesson": True, "last_has_number": True, "last_intensity": 0.5,
         "ends_on_question": False, "face_rate": 1.0, "visual_activity": 0, "scene_cuts": 0, "brief_overlap": 0.2}
    f.update(over)
    return f


def test_spanning_topics_costs_retention_context_and_payoff():
    one, _ = heuristic.score(_features(topics_spanned=1))
    three, why3 = heuristic.score(_features(topics_spanned=3))
    assert three["retention"] <= one["retention"] - 20
    assert three["context"] < one["context"]
    assert three["payoff"] == 50 < one["payoff"]
    assert "later subject" in why3["payoff"]


def test_ad_reads_are_recognised():
    from ezra.scoring.features import AD_READ

    ad = ("This is a brand new book that I co-authored. If you buy the book and enter, we give one person "
          "$1 million. So scan the QR code on screen.")
    assert len(AD_READ.findall(ad)) >= 2
    assert len(AD_READ.findall("They caught Darius ten minutes in and he still wants the money.")) == 0
    scores, why = heuristic.score(_features(ad_read=3), {"brief": "", "min_duration": 15, "max_duration": 60})
    assert scores["campaign_fit"] <= 10 and "ad read" in why["campaign_fit"]


def test_sponsor_segments_and_integrated_brand_mentions():
    from ezra.scoring.features import ad_regions, sponsor_terms

    segs = [seg(500, 504, "I've played a lot of Call of Duty in my day.", "S1"),
            seg(505, 509, "We ran from the cops all night.", "S1"),
            seg(540, 546, "Scan this QR code or click the link in the description to download Call of Duty Mobile.",
                "S1"),
            seg(600, 604, "If you've ever enjoyed a Mr. Beast video, you will love this.", "S1"),
            seg(900, 905, "Darius got caught at the subway.", "S2")]
    regions = ad_regions(segs)
    assert len(regions) == 1 and regions[0][0] <= 540 <= regions[0][1] and regions[0][1] < 900
    terms = sponsor_terms(segs, regions)
    assert {"call of duty mobile", "call of duty"} <= terms and "beast" not in terms


def test_windows_start_only_at_sentence_starts():
    segs = [Segment(0, 3, "and if they arrest us by the end of", speaker="S1", sentence_end=False),
            seg(5, 8, "their lures, but if we escape, they get nothing!", "S1"),
            seg(8, 20, "Start the time and run as fast as you can through the whole city tonight.", "S1"),
            seg(20, 30, "Everyone scattered in a different direction and the cops followed.", "S1")]
    starts = {w.start for w in scout_windows(1, segs, [], 15, 60)}
    assert 5 not in starts and 0 in starts


def test_critic_runs_in_batches_and_survives_a_failed_batch(monkeypatch):
    from types import SimpleNamespace

    from ezra import candidates, llm

    cands = [SimpleNamespace(id=i, start=float(i), end=i + 20.0, context_before="", transcript=f"clip {i}",
                             source_id=1) for i in range(1, 20)]
    seen: list[int] = []

    def fake_call(system, prompt, schema, task, **kw):
        ids = [c.id for c in cands if f"[id {c.id}]" in prompt]
        seen.append(len(ids))
        if 9 in ids:                                   # the second batch times out
            raise llm.LLMError("claude timed out after 300s")
        return llm.LLMResult({"candidates": [{"id": i, "scores": {}, "hook_text": "h", "title": "t",
                                              "hook_type": "other", "reason": "r", "rule_checks": []}
                                             for i in ids]}, "fake", None, 0.1, {})

    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: SimpleNamespace(available=True))
    monkeypatch.setattr(llm, "call", fake_call)
    out = candidates.critic_pass(cands, None)
    assert sorted(seen) == [1, 6, 6, 6]                # 19 candidates -> batches of 6, 6, 6, 1
    assert set(out) == set(range(1, 7)) | set(range(13, 20))   # the failed batch (7-12) keeps heuristic scores


def test_critic_keep_range_becomes_new_bounds(monkeypatch):
    from types import SimpleNamespace

    from ezra import candidates, llm

    segs = [Segment(10, 14, "Yeah, it's just underneath all the money."), Segment(14, 20, "So they ran."),
            Segment(20, 30, "He trapped 100 cops inside a theater with one way out."),
            Segment(30, 40, "Then he locked the door and walked away with the money."),
            Segment(40, 44, "Let us do a lightning round.")]
    monkeypatch.setattr(candidates, "load_segments", lambda sid: segs)
    c = SimpleNamespace(id=1, start=10.0, end=40.0, context_before="", transcript="...", source_id=7)

    def fake_call(system, prompt, schema, task, **kw):
        assert "3. [20.0s] He trapped 100 cops" in prompt and "(after the clip)" in prompt
        return llm.LLMResult({"candidates": [{"id": 1, "scores": {}, "hook_text": "h", "title": "t",
                                              "hook_type": "other", "reason": "r", "keep": {"from": 3, "to": 4},
                                              "rule_checks": []}]}, "fake", None, 0.1, {})

    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: SimpleNamespace(available=True))
    monkeypatch.setattr(llm, "call", fake_call)
    out = candidates.critic_pass([c], None)
    assert out[1]["_bounds"] == (20, 40)              # opens on the strongest line, ends on the payoff


def test_face_timing_and_its_effect_on_hook_and_retention():
    from ezra.scoring.features import _face_timing

    tl = [(float(t), [[0.5, 0.4, 0.1, 0.1]] if t < 3 or t > 15 else []) for t in range(20)]
    assert _face_timing(tl, 0.0) == (True, 13.0)            # face at the start, 13 s faceless (3..15)
    faceful, _ = heuristic.score(_features(opening_face=True, longest_faceless=2.0))
    faceless, why = heuristic.score(_features(opening_face=False, longest_faceless=14.0))
    assert faceful["hook"] - faceless["hook"] == 12
    assert faceless["retention"] < faceful["retention"] and "nobody on screen" in why["retention"]
