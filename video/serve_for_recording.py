#!/usr/bin/env python3
"""Serve the dashboard for recording, with toast notifications and the on-disk
notification state switched off, so a recording run never pops a toast or writes
`.notify_state.json` next to a dashboard you already have running.

    python video/serve_for_recording.py --dir ~/.claude/projects --port 8799
"""
import argparse
import os
import sys
import tempfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_fluency_insights as afi  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=afi.default_root())
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    if not os.path.isdir(a.dir):
        sys.exit("log directory not found: %s" % a.dir)

    afi.send_windows_toast = lambda *args, **kw: None          # never show a toast
    scanner = afi.Scanner(a.dir)
    state = os.path.join(tempfile.mkdtemp(prefix="afi-rec-"), "notify_state.json")
    watcher = afi.NotificationWatcher(scanner, state_path=state)
    try:
        watcher.poll_once()    # one baseline pass so the plan-usage card has data; fires nothing
    except Exception as exc:
        print("[warn] baseline poll failed: %s" % exc)

    afi.Handler.scanner, afi.Handler.root, afi.Handler.watcher = scanner, a.dir, watcher
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), afi.Handler)
    print("recording server on http://127.0.0.1:%d  (logs: %s)" % (a.port, a.dir), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
