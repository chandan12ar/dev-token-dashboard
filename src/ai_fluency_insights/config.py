"""User-tunable settings: pricing, goals, adoption scoring and notification thresholds."""


# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens). Edit freely — these are estimates for the
# "what would this cost on the API" card. Cache write ≈ 1.25x input,
# cache read ≈ 0.1x input.
# ---------------------------------------------------------------------------
PRICING = [
    ("opus",   {"in": 15.0, "out": 75.0}),
    ("sonnet", {"in": 2.0,  "out": 10.0}),
    ("haiku",  {"in": 1.0,  "out": 5.0}),
    ("fable",  {"in": 15.0, "out": 75.0}),
]
DEFAULT_PRICE = {"in": 3.0, "out": 15.0}

# ---------------------------------------------------------------------------
# Optional personal goals / budgets. Set a value to show a progress bar on the
# dashboard; leave 0 to hide that bar. These are targets for YOU — they have
# nothing to do with Anthropic's opaque plan limits.
# ---------------------------------------------------------------------------
GOALS = {
    "daily_loc": 500,        # target lines of code / day
    "daily_tokens": 0,       # output-token budget / day (e.g. 1_000_000)
    "weekly_tokens": 0,      # output-token budget / week (e.g. 5_000_000)
}

# ---------------------------------------------------------------------------
# "My Adoption" tab: how the 0-100 Adoption Score is built. Everything is
# measured over the last `window_days` calendar days (ending on your most
# recent active day) and compared with the window before it. Tweak the
# targets to match your own working rhythm; formulas are in docs/METRICS.md.
# ---------------------------------------------------------------------------
ADOPTION = {
    "window_days": 28,
    "min_active_days": 3,       # fewer active days than this -> "building your baseline"
    "target_days": 12,          # active days per window for full Habit marks (3 a week)
    "features_full": 6,         # distinct features used for full Range marks
    "substantial_actions": 10,  # tool actions that make a session "substantial"
    "loc_target": 300,          # lines Claude wrote per active day for full Impact marks
    "weights": {"habit": 0.25, "range": 0.25, "craft": 0.30, "impact": 0.20},
}
ADOPTION_LEVELS = [
    (0, "Explorer", "You're getting to know Claude Code."),
    (20, "Practitioner", "Claude Code is part of your regular workflow."),
    (40, "Power user", "You hand over real work and check the results."),
    (60, "Expert", "You use the range of Claude Code and it mostly works first time."),
    (75, "Pro", "You work with Claude Code like a skilled teammate: planned, verified, steady."),
    (90, "Pro Max", "Top of the ladder: strong on every pillar, week after week."),
]
# The top two rungs need more than a score: no weak pillar, and (Pro Max) a sustained run.
LEVEL_GATES = {"Pro": {"min_pillar": 50}, "Pro Max": {"min_pillar": 70, "sustain_weeks": 4, "sustain_score": 85}}

# ---------------------------------------------------------------------------
# Live token-usage pop-up notifications for whatever Claude Code session is
# currently running. Independent signals, each fired at most once per
# threshold as it's crossed:
#   - plan_pct:     ALL sessions' tokens combined, as a % of window_ceiling,
#                   within a rolling ~5h window that resets like the Claude
#                   app's "Current session" plan-usage panel. window_ceiling
#                   is a local estimate (Anthropic's real ceiling isn't
#                   queryable) — tune it by eyeballing the app's real %.
#   - context_pct:  latest turn's (input+cache) as a % of context_window
#   - session_step: cumulative session tokens, every flat session_step tokens
#   - task_step:    cumulative tokens since the last user message, every
#                   flat task_step tokens (resets each new user message)
# ---------------------------------------------------------------------------
NOTIFY = {
    "enabled": True,
    "context_window": 0,       # 0 = disabled; was firing per-turn context-fill pops
    # weighted (input-token-equivalent) tokens for one 5h plan window — see
    # weighted_tokens(). Calibrated 2026-09-07 against the real Pro app panel
    # (35% used, resets in 3h16m -> ~104min elapsed): this session's actual
    # weighted usage in that span was ~9.16M, so ceiling = 9.16M / 0.35.
    # One data point on one (cache-heavy coding) session — re-tune if it
    # drifts from the real panel.
    "window_ceiling": 26_000_000,
    "session_step": 0,         # 0 = disabled; redundant with plan_pct's 100% mark
    "task_step": 0,            # 0 = disabled; was firing alongside session_step
    "poll_seconds": 5,
    "active_window_min": 15,   # only watch sessions written to in the last N min
}
MILESTONES = (25, 50, 75, 85, 100)
# Per-kind override for the official rate-limit notifications (see
# NotificationWatcher._poll_official_rate_limits) -- the weekly window is
# watched at a coarser cadence than the 5h "current session" one, which
# uses MILESTONES above.
OFFICIAL_MILESTONES = {
    "five_hour": MILESTONES,
    "seven_day": (20, 40, 60, 80, 100),
}


def should_run_notifications(notify=None):
    notify = NOTIFY if notify is None else notify
    return bool(notify.get("enabled", True))


# Quiet hours for the wellness panel (24h clock, wraps midnight): messages with
# a timestamp >= QUIET_START or < QUIET_END count as late-night activity.
QUIET_START, QUIET_END = 23, 6


# ---------------------------------------------------------------------------
# Optional per-user overrides.
#
# The settings above are the built-in defaults. In a packaged build they are
# compiled into the executable and cannot be edited, so a user (or a company's
# IT team) can override any of them by dropping a `settings.json` in the
# per-user data directory (paths.app_data_dir(); set AFI_DATA_DIR to relocate
# it). Everything is optional — a missing or broken file leaves the defaults
# untouched, and only the keys actually present are changed.
#
# Example settings.json:
#   {
#     "pricing":  {"opus": {"in": 15, "out": 75}, "sonnet": {"in": 2, "out": 10}},
#     "goals":    {"daily_loc": 300},
#     "notify":   {"enabled": false, "window_ceiling": 30000000},
#     "adoption": {"window_days": 14}
#   }
# ---------------------------------------------------------------------------
SETTINGS_FILENAME = "settings.json"


def _merge_dict(target, overrides):
    """Shallow per-key update of a dict in place; ignores non-dict overrides."""
    if isinstance(overrides, dict):
        target.update(overrides)


def apply_overrides(data):
    """Apply an already-parsed settings mapping onto the module's defaults.

    Separate from reading the file so it can be unit-tested without disk I/O.
    Unknown top-level keys are ignored; a malformed section is skipped rather
    than raising, so one bad entry never takes the dashboard down."""
    if not isinstance(data, dict):
        return
    pricing = data.get("pricing")
    if isinstance(pricing, dict):
        # PRICING is an ordered list of (substring, price) pairs. Update the
        # price for a model already listed, append any new model name.
        by_key = {k: p for k, p in PRICING}
        for name, price in pricing.items():
            if isinstance(price, dict) and {"in", "out"} <= price.keys():
                if name in by_key:
                    by_key[name].update(price)
                else:
                    PRICING.append((name, dict(price)))
    _merge_dict(DEFAULT_PRICE, data.get("default_price"))
    _merge_dict(GOALS, data.get("goals"))
    _merge_dict(NOTIFY, data.get("notify"))
    _merge_dict(ADOPTION, data.get("adoption"))


def load_user_settings():
    """Read and apply <data dir>/settings.json if present. Best-effort."""
    try:
        import json
        import os

        from .paths import app_data_dir
        path = os.path.join(app_data_dir(), SETTINGS_FILENAME)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        apply_overrides(data)
        return path
    except Exception:
        # A broken override file must never stop the app from starting.
        return None
