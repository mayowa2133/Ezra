"""OAuth for the CLI without the API running: a one-shot listener on 127.0.0.1 receives the
authorization redirect (Google's recommended flow for installed apps; a "Desktop app" OAuth
client accepts any loopback port). State and PKCE are the same as the API flow."""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

PAGE = ("<html><body style='font-family:system-ui;padding:40px'><h2>{title}</h2><p>{body}</p>"
        "</body></html>")


def wait_for_code(port: int, timeout: float = 300,
                  on_ready: Callable[[str], None] | None = None) -> dict[str, str]:
    """Serve one request on http://127.0.0.1:{port}/callback and return its query parameters."""
    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in parse_qs(url.query).items()})
            ok = "code" in got
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.format(
                title="Connected" if ok else "Not connected",
                body="You can close this tab and return to the terminal." if ok
                else f"The authorization didn't complete: {got.get('error', 'no code')}").encode())
            done.set()

        def log_message(self, *args: Any) -> None:   # keep the terminal quiet; never log the code
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    if on_ready:
        on_ready(f"http://127.0.0.1:{server.server_address[1]}/callback")
    try:
        waited = 0.0
        while not done.is_set() and waited < timeout:
            server.handle_request()
            waited += 1
    finally:
        server.server_close()
    if not done.is_set():
        raise TimeoutError(f"no authorization received within {timeout:.0f}s")
    return got


def connect(provider: str, port: int, open_browser: bool = True, timeout: float = 300,
            show: Callable[[str], None] = print) -> Any:
    from . import connect_finish, connect_start

    redirect = f"http://127.0.0.1:{port}/callback"
    start = connect_start(provider, redirect_uri=redirect)
    show(f"Open this URL to authorize (it was opened in your browser):\n{start['authorize_url']}")
    if open_browser:
        webbrowser.open(start["authorize_url"])
    params = wait_for_code(port, timeout)
    if "code" not in params:
        raise RuntimeError(f"authorization failed: {params.get('error', 'no code returned')}")
    return connect_finish(provider, params["code"], params.get("state", ""), redirect_uri=redirect)
