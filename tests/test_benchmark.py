import os

from ezra import llm
from ezra.benchmark.run import _Home
from ezra.config import get_settings, reset_settings


def test_benchmark_disables_installed_model_clis_and_restores_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EZRA_LLM", "auto")
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm, "_SIGNED_OUT", {})
    reset_settings()
    original_home = os.environ["EZRA_HOME"]
    assert llm.get_llm().available
    with _Home(tmp_path / "benchmark"):
        assert get_settings().llm == "heuristic"
        assert not llm.get_llm().available
    assert os.environ["EZRA_HOME"] == original_home
    assert get_settings().llm == "auto"
    assert llm.get_llm().available
