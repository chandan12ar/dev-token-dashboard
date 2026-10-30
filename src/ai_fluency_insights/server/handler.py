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


class Handler(BaseHTTPRequestHandler):
    scanner: Scanner = None
    root: str = ""
    watcher = None

    def do_GET(self):
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
