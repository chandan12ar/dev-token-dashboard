"""AI Fluency Insights — a fully-local, zero-token-cost dashboard for your Claude Code usage.

It reads the JSONL session logs Claude Code already writes to ~/.claude/projects/
(no API calls, no tokens consumed) and serves a live auto-refreshing dashboard.

Package layout (dependencies only point downwards):

    cli, server        entry points: command line, HTTP server and JSON API
    integrations       Claude Code statusLine setup, `claude -p`, Windows logon task
    notifications      live usage toasts and the plan-usage window
    analytics          the insight engines behind each dashboard tab
    core               log parsing, scanning and the Stats aggregate
    config, paths      user-tunable settings and filesystem locations
"""

__version__ = "1.0.0"
