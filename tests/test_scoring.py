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
