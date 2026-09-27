from types import SimpleNamespace

from ezra import metadata


def _cand(title, transcript, hook=None, hook_type=None):
    return SimpleNamespace(title=title, transcript=transcript, hook=hook, hook_type=hook_type)


def test_headline_prefers_a_written_title_over_the_opening_words():
    spoken = "We're in the middle of the world's deadliest jungle."
    written = _cand("Jungle vs desert vs Antarctica for $100,000", spoken, hook=spoken)
    assert metadata._headline(written) == "Jungle vs desert vs Antarctica for $100,000"
    opening = _cand("We're in the middle of the world's…", "We're in the middle of the world's deadliest jungle.",
                    hook="We're in the middle of the world's deadliest jungle.")
    assert metadata._headline(opening) == "We're in the middle of the world's deadliest jungle."


def test_topic_tags_are_names_or_repeated_words_not_one_off_verbs():
    text = "We're in the Antarctic. Antarctica is cold. We threw up and kept going. The Antarctica wind."
    tags = metadata._topic_tags("MrBeast threw up in Antarctica", text)
    assert "#antarctica" in tags and "#threw" not in tags and "#middle" not in tags
    assert metadata._topic_tags("We struck water", "water water everywhere, water is water") == ["#water"]
    assert metadata.creator_tag("Mr Beast!") == "#mrbeast" and metadata.creator_tag(None) is None


def test_enforce_credits_the_creator_and_keeps_required_tags():
    camp = SimpleNamespace(creator="MrBeast", required_hashtags=["#ad"], required_mentions=["@mrbeast"],
                           required_cta=None)
    meta = metadata.enforce({"tiktok": {"caption": "Who would you bet on?", "hashtags": ["#survival"]}}, camp)
    cap = meta["tiktok"]["caption"]
    assert "🎥 MrBeast" in cap and "#ad" in cap and "@mrbeast" in cap
    assert meta["tiktok"]["hashtags"][0] == "#ad"
    already = metadata.enforce({"tiktok": {"caption": "via MrBeast #ad @mrbeast", "hashtags": []}}, camp)
    assert already["tiktok"]["caption"].count("MrBeast") == 1
    tagged = metadata.enforce({"tiktok": {"caption": "Wow #mrbeast #ad @mrbeast", "hashtags": []}}, camp)
    assert "🎥 MrBeast" not in tagged["tiktok"]["caption"]              # the @mention credits them
    only_tag = metadata.enforce({"tiktok": {"caption": "Wow #mrbeast #ad", "hashtags": []}},
                                SimpleNamespace(**{**vars(camp), "required_mentions": []}))
    assert "🎥 MrBeast" in only_tag["tiktok"]["caption"]
