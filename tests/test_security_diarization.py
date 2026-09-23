import time
from datetime import timedelta

import numpy as np
import pytest

from ezra import db, security
from ezra.db.models import OAuthState
from ezra.diarization import Turn, assign_speakers, cluster, mfcc
from ezra.transcription.base import Word


def test_media_validation(tmp_path, tiny_video, monkeypatch):
    meta = security.validate_media(tiny_video)
    assert meta["has_audio"] and meta["width"] == 640 and 11 < meta["duration"] < 13
    fake = tmp_path / "evil.mp4"
    fake.write_text("not a video")
    with pytest.raises(security.ValidationError):
        security.validate_media(fake)
    with pytest.raises(security.ValidationError):
        security.validate_media(tmp_path / "x.exe")
    monkeypatch.setenv("EZRA_MAX_UPLOAD_MB", "0")
    from ezra.config import reset_settings
    reset_settings()
    with pytest.raises(security.ValidationError, match="exceeds"):
        security.validate_media(tiny_video)


def test_import_path_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("EZRA_IMPORT_ROOTS", str(tmp_path / "allowed"))
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed" / "a.mp4").write_text("x")
    assert security.safe_import_path(tmp_path / "allowed" / "a.mp4")
    for bad in (tmp_path / "other.mp4", tmp_path / "allowed" / ".." / "other.mp4", "/etc/passwd"):
        with pytest.raises(PermissionError):
            security.safe_import_path(bad)


def test_webhook_signatures():
    body = b'{"event":"published"}'
    sig = security.sign(body, "s3cret")
    assert security.verify(body, sig, "s3cret")
    assert not security.verify(body + b" ", sig, "s3cret")
    assert not security.verify(body, sig, "other")
    old = security.sign(body, "s3cret", timestamp=int(time.time()) - 3600)
    assert not security.verify(body, old, "s3cret")


def test_oauth_state_single_use_bound_and_expiring():
    st = security.new_oauth_state("youtube")
    with pytest.raises(PermissionError):
        security.consume_oauth_state(st, "tiktok")
    assert security.consume_oauth_state(st, "youtube").provider == "youtube"
    with pytest.raises(PermissionError, match="already used"):
        security.consume_oauth_state(st, "youtube")
    st2 = security.new_oauth_state("youtube")
    with db.session() as s:
        s.get(OAuthState, st2).created_at -= timedelta(minutes=30)
    with pytest.raises(PermissionError, match="expired"):
        security.consume_oauth_state(st2, "youtube")


def test_safe_redirect():
    assert security.safe_redirect("/review") == "http://localhost:3000/review"
    assert security.safe_redirect("https://evil.example/x") == "http://localhost:3000"
    assert security.safe_redirect("//evil.example") == "http://localhost:3000"


def test_rate_limiter():
    rl = security.RateLimiter(3)
    assert [rl.allow("ip") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("other")


def test_mfcc_and_clustering_separate_two_synthetic_voices():
    sr = 16000
    rng = np.random.default_rng(0)

    def voice(f0: float) -> np.ndarray:
        t = np.arange(int(1.6 * sr)) / sr
        return sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6)) + 0.05 * rng.standard_normal(len(t))

    feats = []
    for i in range(12):
        m = mfcc(voice(110 if i % 2 else 240))
        feats.append(np.concatenate([m.mean(0), m.std(0)]))
    X = np.array(feats)
    X = (X - X.mean(0)) / (X.std(0) + 1e-8)
    labels, conf = cluster(X)
    assert len(set(labels)) == 2 and conf > 0.3
    assert all(labels[i] == labels[i % 2] for i in range(12))


def test_assign_speakers_by_overlap():
    words = [Word("a", 0, 0.5), Word("b", 1.0, 1.4), Word("c", 3.0, 3.5)]
    assign_speakers(words, [Turn(0, 1.2, "S1"), Turn(1.2, 5, "S2")])
    assert [w.spk for w in words] == ["S1", "S1", "S2"]
