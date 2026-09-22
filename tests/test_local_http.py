import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_music_engine.local_http import open_local


def test_loopback_redirect_is_not_followed_and_proxy_is_disabled(monkeypatch):
    visited = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"local")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("no_proxy", "")
        base = f"http://127.0.0.1:{server.server_port}"
        with open_local(urllib.request.Request(base + "/ok"), timeout=2) as response:
            assert response.read() == b"local"
        with pytest.raises(urllib.error.URLError, match="redirects are not allowed"):
            open_local(urllib.request.Request(base + "/redirect"), timeout=2)
        assert visited == ["/ok", "/redirect"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("url", ["https://127.0.0.1/private", "http://example.com/private", "http://user:pass@localhost/private"])
def test_nonlocal_or_credentialed_urls_are_rejected_before_io(url):
    with pytest.raises(ValueError):
        open_local(urllib.request.Request(url), timeout=1)
