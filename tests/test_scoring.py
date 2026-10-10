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


def test_llm_using_scopes_the_provider_and_rejects_unknown_names():
    import pytest

    from ezra import llm

    assert llm.get_llm().name == "heuristic"
    with llm.using("claude"):
        assert llm.get_llm().name == "claude-cli"
        with llm.using(None):
            assert llm.get_llm().name == "claude-cli"          # None keeps the outer choice
    assert llm.get_llm().name == "heuristic"
    with pytest.raises(ValueError, match="unknown agent"), llm.using("gpt-9"):
        pass
    assert llm.get_llm().name == "heuristic"


def test_demonstratives_that_point_are_not_dangling_and_segues_are_not_endings():
    from ezra.candidates import _segue, story_similarity
    from ezra.scoring.features import _dangling, _first_words

    assert not _dangling(_first_words("This field behind me will be a city", 3))
    assert not _dangling(_first_words("This is the bathroom for the community", 3))
    assert _dangling(_first_words("That's why we left", 3)) and _dangling(_first_words("It was huge", 3))
    assert _segue("Anyway, let's build a football field.") and _segue("Now that the sun is up, go.")
    assert not _segue("And they had an entire list.")
    same = story_similarity("Isaac asked for white tiles in his new house", "Isaac, you asked for white tiles")
    other = story_similarity("Isaac asked for white tiles in his new house", "The current swept the raft away")
    assert same > 0.35 > other


def test_auto_llm_prefers_a_signed_in_local_cli(monkeypatch):
    from ezra import llm
    from ezra.config import reset_settings

    monkeypatch.setenv("EZRA_LLM", "auto")
    reset_settings()
    monkeypatch.setattr(llm, "_SIGNED_OUT", {})
    on_path: set[str] = set()
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}" if name in on_path else None)
    assert llm.get_llm().name == "heuristic"                  # nothing installed: no model, no error
    on_path.add("codex")
    assert llm.get_llm().name == "codex-cli"
    on_path.add("claude")
    assert llm.get_llm().name == "claude-cli"
    with llm.using("none"):
        assert llm.get_llm().name == "heuristic"              # --agent none still turns the model off
    llm._SIGNED_OUT["claude-cli"] = "401"
    assert llm.get_llm().name == "codex-cli"                  # a signed-out CLI is skipped


def test_a_signed_out_cli_is_dropped_after_one_call(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from ezra import llm

    monkeypatch.setattr(llm, "_SIGNED_OUT", {})
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    calls: list[int] = []

    def fake_run(*a, **k):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout="", stderr="API Error: 401 Invalid authentication credentials")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = llm.ClaudeCLIProvider()
    with pytest.raises(llm.LLMUnavailable, match="not signed in"):
        provider.complete_json("s", "p", {"type": "object"}, "t")
    assert not provider.available and not llm.ClaudeCLIProvider().available and len(calls) == 1


def _sentences(n: int, length: float = 6.0):
    from ezra.transcription.base import Segment, Word

    return [Segment(i * length, (i + 1) * length - 0.3, f"Sentence {i} about the money we lost.",
                    [Word(f"w{i}", i * length, (i + 1) * length - 0.3)]) for i in range(n)]


def test_fit_range_snaps_to_sentences_and_duration_limits():
    from ezra.candidates import _fit_range

    segs = _sentences(30)
    segs[4].sentence_end = False                      # sentence 4 runs on into 5
    assert _fit_range(segs, 5, 7, 15, 60) == (4, 7)   # start pulled back to where the sentence starts
    assert _fit_range(segs, 0, 0, 15, 60) == (0, 2)   # too short: extended to reach 15 s
    assert _fit_range(segs, 0, 20, 15, 60) == (0, 9)  # too long: trimmed to fit 60 s
    assert _fit_range(_sentences(1), 0, 0, 15, 60) is None


def test_model_proposals_read_the_whole_transcript_and_merge_across_chunks(monkeypatch):
    from types import SimpleNamespace

    from ezra import candidates, llm

    segs = _sentences(300)                            # two overlapping chunks (0-160, 140-300)
    prompts: list[str] = []

    def fake_call(system, prompt, schema, task, **kw):
        prompts.append(prompt)
        first = int(prompt.split("\n  1. [")[1].split("s]")[0].split(".")[0]) // 6
        moments = []
        for absolute, strength in ((10, 70), (150, 90), (151, 60), (290, 40)):
            if first <= absolute < first + candidates.PROPOSER_CHUNK:
                n = absolute - first + 1
                moments.append({"from": n, "to": n + 3, "strength": strength, "hook_text": f"hook {absolute}",
                                "reason": "stakes"})
        moments.append({"from": 999, "to": 1000, "strength": 99, "hook_text": "x", "reason": "out of range"})
        return llm.LLMResult({"moments": moments}, "fake", None, 0.1, {})

    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: SimpleNamespace(available=True))
    monkeypatch.setattr(llm, "call", fake_call)
    out = candidates.propose_windows(1, segs, [], None, 15, 60, prompt="money lost")
    assert len(prompts) == 2 and all("This run is looking for: money lost" in p for p in prompts)
    starts = [round(w.start) for w, _ in out]
    # strongest first; 150 is seen by both chunks but kept once; 151 is a re-cut of 150 and is dropped
    assert starts == [900, 60, 1740]
    assert out[0][1]["hook_text"] == "hook 150" and out[0][1]["provider"] == "fake"


def test_no_model_means_no_proposals(monkeypatch):
    from ezra import candidates, llm

    called: list[int] = []
    monkeypatch.setattr(llm, "call", lambda *a, **k: called.append(1))
    assert candidates.propose_windows(1, _sentences(40), [], None, 15, 60) == [] and not called


def test_find_candidates_shortlists_model_proposals_first(tiny_video, monkeypatch):
    """No speech model needed: a synthetic transcript stands in for the analysis."""
    from types import SimpleNamespace

    from ezra import analysis, campaigns, candidates, db, llm, sources
    from ezra.db.models import Source
    from tests.conftest import DEMO_YAML

    campaigns.import_text(DEMO_YAML)
    src = sources.ingest(tiny_video, "demo", rights_basis="campaign_supplied")
    with db.session() as s:
        row = s.get(Source, src.id)
        row.status, row.duration = "analyzed", 600.0
    segs = _sentences(100)
    monkeypatch.setattr(candidates, "load_segments", lambda sid: segs)
    monkeypatch.setattr(analysis, "get", lambda sid, kind: SimpleNamespace(data={"topics": []}))
    monkeypatch.setattr(analysis, "all_for", lambda sid: {})
    monkeypatch.setattr(analysis, "quiet_regions", lambda sid: [])

    def fake_call(system, prompt, schema, task, **kw):
        if task == "clip_proposer":
            return llm.LLMResult({"moments": [{"from": 50, "to": 53, "strength": 90, "hook_text": "We lost it all",
                                               "reason": "stakes"}]}, "fake", None, 0.1, {})
        ids = [int(x.split("]")[0]) for x in prompt.split("[id ")[1:]]
        return llm.LLMResult({"candidates": [{"id": i, "scores": {k: 60 for k in candidates.FACTORS},
                                              "hook_text": "h", "title": "t", "hook_type": "other", "reason": "r",
                                              "keep": {"from": 1, "to": 99}, "rule_checks": []} for i in ids]},
                             "fake", None, 0.1, {})

    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: SimpleNamespace(available=True, name="fake"))
    monkeypatch.setattr(llm, "call", fake_call)
    out = candidates.find_candidates(src.id, "demo", max_candidates=5, prompt="losing money")
    assert len(out) == 5 and out[0].origin == "model" and all(c.origin == "scout" for c in out[1:])
    picked = candidates.get(out[0].id)
    assert abs(picked.start - 300.0) < 7                         # sentence 50, snapped to its word edge
    assert picked.score_explanations["proposal"] | {} == {"strength": 90.0, "hook_text": "We lost it all",
                                                          "reason": "stakes", "provider": "fake",
                                                          "prompt": "losing money"}
    assert picked.scorer == "fake+heuristic"                   # the critic scored the model's own pick
    assert not any(c.start < 323.7 and c.end > 300.0 for c in out[1:])     # no heuristic re-cut of it
    again = candidates.find_candidates(src.id, "demo", max_candidates=5)
    everything = candidates.list_candidates(source_id=src.id, include_failed=True)
    assert len(again) == len(everything) == 5                 # a re-run replaces unrendered model picks
    assert sum(c.origin == "model" for c in everything) == 1
