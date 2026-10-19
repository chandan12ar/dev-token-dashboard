"""Serve the dashboard for a recording with no side effects.

Turns the notification watcher off (so no toast fires and no .notify_state.json is
written next to the script) and never opens a browser.

    python video/serve_for_recording.py --dir /tmp/sample-claude/projects --port 8799
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_fluency_insights as afi  # noqa: E402

afi.NOTIFY["enabled"] = False
if "--no-browser" not in sys.argv:
    sys.argv.append("--no-browser")
afi.main()
