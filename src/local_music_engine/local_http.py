"""HTTP transport for private lyrics and audio: direct loopback, no redirects."""

import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise urllib.error.URLError("local API redirects are not allowed")


def open_local(request: urllib.request.Request, *, timeout: float):
    url = urllib.parse.urlsplit(request.full_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local API must use an http loopback address")
    if url.username or url.password or url.fragment:
        raise ValueError("local API URL must not contain credentials or a fragment")
    # A configured system/environment proxy must never receive private local input.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)
