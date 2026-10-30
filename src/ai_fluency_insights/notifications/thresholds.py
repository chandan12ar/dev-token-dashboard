"""Token-usage notification thresholds (pure functions, no I/O)."""

from ..config import MILESTONES, NOTIFY

# ---------------------------------------------------------------------------
# Token-usage notification thresholds (pure functions — no I/O, easy to test)
# ---------------------------------------------------------------------------

def milestones_crossed(prev_value, new_value, total, milestones=MILESTONES):
    """Milestone percentages (of `total`) whose threshold lies in
    (prev_value, new_value]. Comparing prev vs. new (rather than tracking
    "already fired" state) means a threshold naturally refires if usage
    drops below it (e.g. a context compaction) and climbs back past it."""
    if not total or total <= 0:
        return []
    crossed = []
    for m in milestones:
        thresh = total * m / 100.0
        if prev_value < thresh <= new_value:
            crossed.append(m)
    return crossed


def steps_crossed(prev_total, new_total, step):
    """Flat multiples of `step` whose value lies in (prev_total, new_total]."""
    if not step or step <= 0:
        return []
    prev_n = int(prev_total // step)
    new_n = int(new_total // step)
    if new_n <= prev_n:
        return []
    return [step * n for n in range(prev_n + 1, new_n + 1)]


def weighted_tokens(input_tokens, output_tokens, cache_write, cache_read):
    """Quota-weighted usage in input-token-equivalent units, using the same
    relative weights as PRICING (every tier prices output at 5x input,
    cache write at 1.25x, cache read at 0.1x) — a raw token sum wildly
    overcounts a cache-heavy coding session against Anthropic's real
    rolling-window quota, since cache reads are far cheaper than fresh input."""
    return (input_tokens + output_tokens * 5
            + cache_write * 1.25 + cache_read * 0.1)


class SessionTokenTracker:
    """Replays a Claude Code session's usage events in order and reports
    newly-crossed notification thresholds. Stateless besides the three
    running totals, so it can be resumed after a restart by passing in the
    previously persisted totals — avoids re-firing thresholds already seen."""

    def __init__(self, notify=NOTIFY, session_total=0, task_total=0,
                 last_context_usage=0):
        self.notify = notify
        self.session_total = session_total
        self.task_total = task_total
        self.last_context_usage = last_context_usage

    def on_user_message(self):
        """Call on each new real (non-tool-result) user message — starts a
        new task, so the task_step counter resets."""
        self.task_total = 0

    def on_usage(self, input_tokens, output_tokens, cache_write, cache_read):
        events = []
        added = input_tokens + output_tokens + cache_write + cache_read
        context_now = input_tokens + cache_write + cache_read

        prev_context = self.last_context_usage
        self.last_context_usage = context_now
        for m in milestones_crossed(prev_context, context_now,
                                     self.notify["context_window"]):
            events.append(("context_pct", m, context_now))

        prev_session = self.session_total
        self.session_total += added
        for step_val in steps_crossed(prev_session, self.session_total,
                                       self.notify["session_step"]):
            events.append(("session_step", step_val, self.session_total))

        prev_task = self.task_total
        self.task_total += added
        for step_val in steps_crossed(prev_task, self.task_total,
                                       self.notify["task_step"]):
            events.append(("task_step", step_val, self.task_total))

        return events

    def state(self):
        return {"session_total": self.session_total,
                "task_total": self.task_total,
                "last_context_usage": self.last_context_usage}
