"""HTTP routing for the local dashboard: the web UI, its static assets and the JSON API."""

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from ..core.scanner import Scanner
from ..paths import resource_path
from .api import ai_summary_payload, reflect_payload, stats_payload

WEB_DIR = resource_path("web")
STATIC_DIR = os.path.join(WEB_DIR, "static")
CHART_JS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"
_TYPES = {".js": "application/javascript", ".css": "text/css; charset=utf-8",
          ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
# Only requests whose Host/Origin name the loopback interface are served. This
# blocks DNS-rebinding: a page on evil.com whose domain is repointed at
# 127.0.0.1 still sends "Host: evil.com", and another site's fetch to us
# carries its own "Origin:" — both are rejected, so no web page can read your
# stats or trigger the token-spending `claude -p` routes. (Finding F5.)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _hostname(value):
    """The host part of a Host/authority or an Origin URL, lowercased, no port."""
    if not value:
        return ""
    v = value.strip().lower()
    if "://" in v:
        v = urlparse(v).netloc
    if v.startswith("["):            # [::1]:8787  ->  ::1
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if ":" in v else v


class Handler(BaseHTTPRequestHandler):
    scanner: Scanner = None
    root: str = ""
    watcher = None

    def _local_only(self):
        """True if this request is same-origin/loopback; else answers 403."""
        if _hostname(self.headers.get("Host")) not in _LOCAL_HOSTS:
            self._send(403, "text/plain", b"forbidden")
            return False
        origin = self.headers.get("Origin")
        if origin and _hostname(origin) not in _LOCAL_HOSTS:
            self._send(403, "text/plain", b"forbidden")
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path == "/":
            self._send_file(os.path.join(WEB_DIR, "index.html"))
        elif parsed.path.startswith("/static/"):
            self._send_static(parsed.path[len("/static/"):])
        elif parsed.path == "/api/stats":
            frm = (q.get("from") or [""])[0].strip()
            to = (q.get("to") or [""])[0].strip()
            try:
                days = int(q.get("days", ["30"])[0])
            except ValueError:
                days = 30
            data = stats_payload(self.scanner, self.root, self.watcher, days, frm, to)
            self._send_json(data)
        elif parsed.path == "/api/ai_summary":
            self._send_json(ai_summary_payload(self.scanner, (q.get("week") or [""])[0]))
        elif parsed.path == "/api/reflect":
            question = (q.get("q") or [""])[0].strip()[:500]
            self._send_json(reflect_payload(self.scanner, (q.get("week") or [""])[0], question))
        else:
            self._send(404, "text/plain", b"not found")

    def _send_static(self, rel):
        path = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(os.path.realpath(STATIC_DIR) + os.sep):
            self._send(404, "text/plain", b"not found")
        elif not os.path.isfile(path) and rel == "vendor/chart.umd.min.js":
            # fall back to the CDN if the bundled copy is missing
            self.send_response(302)
            self.send_header("Location", CHART_JS_CDN)
            self.end_headers()
        else:
            self._send_file(path)

    def _send_file(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, "text/plain", b"not found")
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = _TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        self._send(200, ctype, body)

    def _send_json(self, payload):
        self._send(200, "application/json", json.dumps(payload).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence request logging
        pass
