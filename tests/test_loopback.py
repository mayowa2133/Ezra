from ezra import publishing
from ezra.publishing import loopback


def test_authorization_is_shown_only_after_callback_listener_is_ready(monkeypatch):
    events = []
    monkeypatch.setattr(publishing, "connect_start", lambda *a, **k: {"authorize_url": "https://example.test/auth"})
    monkeypatch.setattr(publishing, "connect_finish", lambda *a, **k: events.append("connected"))
    monkeypatch.setattr(loopback.webbrowser, "open", lambda url: events.append("opened"))

    def wait_for_code(port, timeout, on_ready):
        assert not events
        events.append("listening")
        on_ready(f"http://127.0.0.1:{port}/callback")
        return {"code": "test-code", "state": "test-state"}

    monkeypatch.setattr(loopback, "wait_for_code", wait_for_code)
    loopback.connect("youtube", 8765, show=lambda msg: events.append("shown"))
    assert events == ["listening", "shown", "opened", "connected"]
