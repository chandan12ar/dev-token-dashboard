"""Plan-usage window: official rate-limit capture plus the local fallback estimate."""

import json
import os
import time

from .thresholds import milestones_crossed

WINDOW_SECONDS = 5 * 3600

# Official rate-limit data, captured by a Claude Code statusLine hook (see
# ~/.claude/statusline.js) and dropped as JSON every time the status line
# renders — the exact 5h/7d plan-usage percentage Anthropic's backend
# reports, not the ceiling guess PlanWindowTracker below makes. Preferred
# over the estimate whenever it's fresh. The status line only updates while
# a session is open and actively rendering, so a stale file (older than
# RATE_LIMITS_STALE_SECONDS) is treated as unavailable and we fall back to
# the estimate rather than show a number that's stopped moving.
RATE_LIMITS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "rate_limits_latest.json")
RATE_LIMITS_STALE_SECONDS = 30 * 60


def load_rate_limits(path, now=None):
    now = time.time() if now is None else now
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if now - data.get("captured_at", 0) > RATE_LIMITS_STALE_SECONDS:
        return None
    return data


def load_rate_limits_any_age(path):
    """Like load_rate_limits(), but never discards the file for being old.

    The statusline hook only re-captures when Claude Code is actively used
    on this machine (see statusline.js), so a quiet stretch longer than
    RATE_LIMITS_STALE_SECONDS makes load_rate_limits() return None. But a
    real Anthropic-reported percentage from an hour ago is still a better
    basis for plan_window_snapshot()'s fallback than PlanWindowTracker's
    single-session-calibrated ceiling guess -- callers pair this with each
    window's `resets_at` to tell a merely-old number from one that's since
    reset to 0%. Only a missing or corrupt file returns None here."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _official_pct_now(window, now):
    """A rate-limit window's used_percentage, adjusted for a capture that's
    old enough its `resets_at` has already passed -- i.e. the account-side
    window has since reset to 0% even though the last capture we have on
    disk still shows whatever it was at right before the reset. Without
    this, an old-but-real capture could be *more* misleading than the local
    estimate it's meant to replace."""
    pct = window.get("used_percentage")
    if pct is None:
        return None
    resets_at = window.get("resets_at")
    if resets_at is not None and now >= resets_at:
        return 0
    return pct


class PlanWindowTracker:
    """Approximates the Claude app's 'Current session' plan-usage panel: a
    single GLOBAL rolling window (combining every Claude Code session, not
    per-session like SessionTokenTracker) that starts on the first usage
    event seen and resets fully — not a sliding average — once WINDOW_SECONDS
    has elapsed, mirroring the app's hard reset rather than a decaying one.
    `ceiling` is a local, user-tuned estimate; Anthropic's real per-window
    token ceiling isn't queryable locally. Used only as a fallback when
    load_rate_limits() has no fresh official number (see RATE_LIMITS_PATH)."""

    def __init__(self, ceiling, window_start=None, window_total=0):
        self.ceiling = ceiling
        self.window_start = window_start
        self.window_total = window_total

    def add(self, ts_epoch, tokens):
        if self.window_start is None or ts_epoch - self.window_start >= WINDOW_SECONDS:
            self.window_start = ts_epoch
            self.window_total = 0
        prev_total = self.window_total
        self.window_total += tokens
        return milestones_crossed(prev_total, self.window_total, self.ceiling)

    def pct(self, now_epoch):
        # add() only rolls window_start/window_total over to a fresh window
        # when a *new* usage event arrives. During an idle stretch spanning
        # the 5h boundary, nothing has called add() yet, so without this
        # check pct() would keep reporting the previous, already-expired
        # window's leftover percentage (while resets_in_seconds() correctly
        # says the window is already over) -- e.g. "8% used, resets in 0h 0m".
        if not self.ceiling or self.window_start is None:
            return 0
        if now_epoch - self.window_start >= WINDOW_SECONDS:
            return 0
        return min(100, int(self.window_total / self.ceiling * 100))

    def resets_in_seconds(self, now_epoch):
        if self.window_start is None:
            return 0
        return max(0, int(self.window_start + WINDOW_SECONDS - now_epoch))

    def state(self):
        return {"window_start": self.window_start, "window_total": self.window_total}
