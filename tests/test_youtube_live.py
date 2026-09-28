"""Live YouTube test: skipped unless EZRA_LIVE_YOUTUBE_TEST=1 (never in ordinary CI).
Needs real credentials and the consent screen; see scripts/youtube_live_test.py."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("EZRA_LIVE_YOUTUBE_TEST") != "1",
                                reason="live YouTube test: set EZRA_LIVE_YOUTUBE_TEST=1 and real credentials")


def test_live_private_upload_round_trip():
    script = Path(__file__).parent.parent / "scripts" / "youtube_live_test.py"
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=900,
                         env={**os.environ, "EZRA_HOME": os.environ.get("EZRA_LIVE_HOME", os.environ["EZRA_HOME"])})
    assert out.returncode == 0, out.stdout + out.stderr
