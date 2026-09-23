import subprocess
from pathlib import Path

import pytest

from clipper import campaigns
from clipper.clipping import sources
from clipper.clipping.transcribe import import_transcript
from clipper.clipping.transcript import Transcript

CAMPAIGN_YAML = """
campaign:
  id: campaign-184
  name: Podcast XYZ
  platform: Content Rewards
  rate:
    cpm: 2.00
  minimum_views: 5000
  maximum_payout: 1000
  requirements:
    min_duration: 15
    max_duration: 60
  forbidden:
    - profanity
    - competitor mentions
  competitors: [Acme]
  required:
    - creator visible
    - subtitles
    - campaign hashtag
  hashtags: ["#xyzpod"]
"""

SENTENCES = [
    "He lost four hundred thousand dollars overnight and nobody warned him.",
    "Nobody tells founders this but the first year is the loneliest part.",
    "I almost quit the company twice before we found product market fit.",
    "This part is damn boring filler about the weather and the coffee.",
    "Acme tried to copy us and it did not work out for them at all.",
]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPER_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
    return tmp_path / "data"


def fake_transcript(duration: float = 90.0) -> Transcript:
    """Words every 0.45s cycling through SENTENCES, one segment per sentence."""
    segments, t, i = [], 0.0, 0
    while t < duration - 6:
        words = []
        for w in SENTENCES[i % len(SENTENCES)].split():
            words.append({"w": w, "s": round(t, 3), "e": round(t + 0.35, 3)})
            t += 0.45
        segments.append({"start": words[0]["s"], "end": words[-1]["e"],
                         "text": " ".join(x["w"] for x in words), "words": words})
        t += 0.8
        i += 1
    return Transcript("en", duration, segments)


@pytest.fixture(scope="session")
def video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "episode-142.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "90",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
        check=True)
    return path


@pytest.fixture
def campaign(video):
    spec = campaigns.parse_yaml(CAMPAIGN_YAML)
    camp = campaigns.create(spec)
    src = sources.add(camp["slug"], str(video))
    import_transcript(src["id"], fake_transcript())
    return {"campaign": camp, "source": sources.get(src["id"])}
