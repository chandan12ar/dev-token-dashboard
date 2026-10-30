"""Runs the dashboard: HTTP server on localhost plus the background notification watcher."""

import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

from ..config import NOTIFY, should_run_notifications
from ..core.scanner import Scanner
from ..integrations.statusline import startup_hint
from ..notifications.watcher import NotificationWatcher
from .handler import Handler


def serve(root, port=8787, open_browser=True):
    scanner = Scanner(root)
    watcher = NotificationWatcher(scanner)

    Handler.scanner = scanner
    Handler.root = root
    Handler.watcher = watcher
    # Bound to loopback only: the dashboard is personal and never exposed on the network.
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"  AI Fluency Insights running at {url}")
    print(f"  Reading logs from: {root}")
    hint = startup_hint()
    if hint:
        print(hint)
    print("  Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def _notify_loop():
        while True:
            try:
                watcher.poll_once()
            except Exception as e:
                print(f"[warn] notification watcher error: {e}")
            time.sleep(NOTIFY["poll_seconds"])

    if should_run_notifications():
        threading.Thread(target=_notify_loop, daemon=True).start()
    else:
        print('  Toast notifications: OFF (NOTIFY["enabled"] is False)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye!")
