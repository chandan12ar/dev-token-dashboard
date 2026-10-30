"""Background watcher that fires usage notifications for live sessions."""

import os
import threading
import time
from datetime import datetime

from ..config import NOTIFY, OFFICIAL_MILESTONES
from ..paths import notify_state_path
from .plan_usage import (
    RATE_LIMITS_PATH,
    PlanWindowTracker,
    _official_pct_now,
    load_rate_limits,
    load_rate_limits_any_age,
)
from .state import load_notify_state, save_notify_state
from .thresholds import SessionTokenTracker, weighted_tokens
from .toast import send_windows_toast


def _fmt_k(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


class NotificationWatcher:
    """Polls the Scanner for sessions being actively written to and fires
    token-usage notifications as SessionTokenTracker crosses thresholds.

    Progress is persisted to `state_path` so restarting the dashboard mid
    -session doesn't re-fire thresholds already seen. A session's very first
    sighting (either on disk from a prior run, or freshly discovered) is
    replayed silently to build an accurate baseline before any events fire —
    otherwise reopening the dashboard mid-session would immediately dump a
    burst of "you already passed 25/50/75%" notifications.
    """

    def __init__(self, scanner, notify=NOTIFY, state_path=None, rate_limits_path=None):
        self.scanner = scanner
        self.notify = notify
        self.state_path = state_path or notify_state_path()
        self.rate_limits_path = rate_limits_path or RATE_LIMITS_PATH
        saved = load_notify_state(self.state_path)
        self._raw_state = saved.get("sessions", {})
        # last-seen official five_hour/seven_day used_percentage, so a
        # restart doesn't refire milestones already crossed before it -- same
        # "baseline on first sight" reasoning as _seen_counts/baseline_sids.
        self._official_prev = dict(saved.get("official_prev", {}))
        # which milestones have already fired *within the account's current
        # window* for each kind, plus that window's resets_at -- unlike
        # _official_prev's simple prev<thresh<=new check, this survives the
        # officially-reported used_percentage wobbling by a point or two
        # between statusline captures (not a true drop-and-reclimb like a
        # context compaction) without refiring the same milestone repeatedly.
        # Cleared only when resets_at itself changes, i.e. a genuinely new
        # window has started.
        self._official_fired = {k: set(v) for k, v in
                                 (saved.get("official_fired") or {}).items()}
        self._official_window_reset_at = dict(saved.get("official_window_reset_at", {}))
        # how many entries of each file were already replayed *before* this
        # process started — restoring this is what stops a restart from
        # replaying a session's whole history on top of its already-saved
        # cumulative total (which would double-count it, and can also mean
        # firing thousands of already-past thresholds in one synchronous
        # burst for a long-running session).
        self._seen_counts = dict(saved.get("seen_counts", {}))
        self.trackers = {}       # sid -> SessionTokenTracker
        self.meta = {}           # sid -> {"project":..., "title":...}
        self.recent_events = []  # for the /api/stats "notifications" field
        self._lock = threading.Lock()
        pw = saved.get("plan_window") or {}
        self.plan_tracker = PlanWindowTracker(
            ceiling=self.notify["window_ceiling"],
            window_start=pw.get("window_start"),
            window_total=pw.get("window_total", 0))

    def _tracker_for(self, sid):
        t = self.trackers.get(sid)
        if t is None:
            saved = self._raw_state.get(sid, {})
            t = SessionTokenTracker(
                notify=self.notify,
                session_total=saved.get("session_total", 0),
                task_total=saved.get("task_total", 0),
                last_context_usage=saved.get("last_context_usage", 0),
            )
            self.trackers[sid] = t
        return t

    @staticmethod
    def _is_real_user_text(content):
        # Failed tool results ride back on "user"-type entries (see
        # parse_entry) — a block-list made up entirely of tool_result isn't
        # a real user message and shouldn't reset the per-task counter.
        if isinstance(content, list):
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in content):
                return False
            return True
        return bool(content)

    def poll_once(self):
        if not self.notify.get("enabled"):
            return
        with self.scanner._lock:
            self.scanner.scan()
            entries_by_path = dict(self.scanner._entries)
        cutoff = time.time() - self.notify["active_window_min"] * 60
        # Snapshot which sessions we already knew about *before* this poll
        # cycle touches anything — a session's logs can span multiple files,
        # and computing this per-file (against a live-mutating self.trackers)
        # would let a session slip past the "first sight" baseline check on
        # whichever file happens to be processed second.
        known_before_poll = set(self._raw_state) | set(self.trackers)
        # Coalesce every threshold crossed this poll into one entry per
        # (sid, kind) — a burst of catch-up entries (dashboard was down, or
        # several turns landed between polls) can cross the same kind of
        # threshold repeatedly, and firing a toast per crossing turns one
        # real event into a rapid-fire stack of near-identical popups.
        pending = {}
        # (ts_epoch, tokens) for every usage event this poll, across every
        # session — the plan window is account-wide, not per session, so it
        # has to see all of them merged in true chronological order.
        plan_events = []
        for path, entries in entries_by_path.items():
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            project = os.path.basename(os.path.dirname(path)) or "unknown"
            project = project.lstrip("-").replace("--", "/").replace("-", "/")
            self._replay(path, entries, project, known_before_poll, pending, plan_events)
        for (sid, kind), (value, total) in pending.items():
            self._fire(sid, kind, value, total)
        plan_events.sort(key=lambda e: e[0])
        plan_fire = None
        for ts_epoch, tokens in plan_events:
            crossed = self.plan_tracker.add(ts_epoch, tokens)
            if crossed:
                plan_fire = (crossed[-1], self.plan_tracker.window_total)
        if plan_fire is not None:
            self._fire_plan(*plan_fire)
        self._poll_official_rate_limits()
        self._save_state()

    def _poll_official_rate_limits(self):
        data = load_rate_limits(self.rate_limits_path)
        if not data:
            return
        rl = data.get("rate_limits") or {}
        for kind, label in (("five_hour", "Current 5h session"), ("seven_day", "This week")):
            window = rl.get(kind)
            if not window or window.get("used_percentage") is None:
                continue
            pct = window["used_percentage"]
            resets_at = window.get("resets_at")
            milestones = OFFICIAL_MILESTONES[kind]

            if self._official_window_reset_at.get(kind) != resets_at:
                self._official_window_reset_at[kind] = resets_at
                self._official_fired[kind] = set()
            fired = self._official_fired.setdefault(kind, set())

            prev = self._official_prev.get(kind)
            if prev is not None:
                eligible = [m for m in milestones if pct >= m and m not in fired]
                if eligible:
                    milestone = max(eligible)
                    fired.update(m for m in milestones if m <= milestone)
                    self._fire_official(kind, label, milestone, pct, resets_at)
            else:
                # first sighting of this kind: seed the baseline without
                # firing, same "don't dump history on open" reasoning as
                # baseline_sids in _replay.
                fired.update(m for m in milestones if pct >= m)
            self._official_prev[kind] = pct

    def _replay(self, path, entries, project, known_before_poll, pending, plan_events):
        seen = self._seen_counts.get(path, 0)
        new_entries = entries[seen:]
        self._seen_counts[path] = len(entries)
        if not new_entries:
            return
        baseline_sids = ({e.get("sessionId") for e in new_entries if e.get("sessionId")}
                          - known_before_poll)
        for entry in new_entries:
            etype = entry.get("type")
            sid = entry.get("sessionId")
            if etype == "ai-title" and sid:
                title = entry.get("aiTitle")
                if title:
                    self.meta.setdefault(sid, {})["title"] = title
                continue
            if etype not in ("user", "assistant") or entry.get("isMeta") or not sid:
                continue
            meta = self.meta.setdefault(sid, {})
            meta.setdefault("project", project)
            tracker = self._tracker_for(sid)
            if etype == "user":
                content = (entry.get("message") or {}).get("content")
                if self._is_real_user_text(content):
                    tracker.on_user_message()
                continue
            usage = (entry.get("message") or {}).get("usage") or {}
            if not usage:
                continue
            input_tokens = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            cache_write = usage.get("cache_creation_input_tokens", 0) or 0
            cache_read = usage.get("cache_read_input_tokens", 0) or 0
            events = tracker.on_usage(input_tokens, output_tokens, cache_write, cache_read)
            if sid in baseline_sids:
                continue
            for kind, value, total in events:
                pending[(sid, kind)] = (value, total)
            # a brand-new session's own first sighting still skips the plan
            # window too — same reasoning as baseline_sids above, otherwise
            # reopening the dashboard mid-session would dump that session's
            # entire history into the account-wide total in one shot.
            try:
                ts_epoch = datetime.fromisoformat(
                    (entry.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            plan_events.append((ts_epoch, weighted_tokens(
                input_tokens, output_tokens, cache_write, cache_read)))

    def _fire(self, sid, kind, value, total):
        meta = self.meta.get(sid, {})
        title = meta.get("title") or "Untitled session"
        project = meta.get("project", "unknown")
        label = {
            "context_pct": f"Context {value}% full ({_fmt_k(total)} / "
                            f"{_fmt_k(self.notify['context_window'])} tokens)",
            "session_step": f"Session has used {_fmt_k(total)} tokens (+{_fmt_k(value)})",
            "task_step": f"Current task has used {_fmt_k(total)} tokens (+{_fmt_k(value)})",
        }[kind]
        event = {
            "id": f"{sid}:{kind}:{value}",
            "ts": time.time(),
            "project": project,
            "title": title,
            "message": label,
        }
        with self._lock:
            self.recent_events.append(event)
            del self.recent_events[:-50]
        send_windows_toast(f"{project} — {title}"[:64], label)

    def _fire_plan(self, value, total):
        resets_in = self.plan_tracker.resets_in_seconds(time.time())
        h, m = divmod(resets_in // 60, 60)
        label = (f"Current session {value}% used ({_fmt_k(total)} / "
                 f"{_fmt_k(self.notify['window_ceiling'])} tokens, est.) — "
                 f"resets in {h}h {m}m")
        event = {
            "id": f"plan:{value}:{int(total)}",
            "ts": time.time(),
            "project": "plan",
            "title": "Plan usage (estimate)",
            "message": label,
        }
        with self._lock:
            self.recent_events.append(event)
            del self.recent_events[:-50]
        send_windows_toast("Plan usage limits (estimate)", label)

    def _fire_official(self, kind, label, milestone, pct, resets_at):
        resets_txt = ""
        if resets_at:
            resets_in = max(0, int(resets_at - time.time()))
            h, m = divmod(resets_in // 60, 60)
            resets_txt = f" — resets in {h}h {m}m"
        message = f"{label} {milestone}% used (official){resets_txt}"
        event = {
            "id": f"official:{kind}:{milestone}",
            "ts": time.time(),
            "project": "plan",
            "title": f"{label} usage",
            "message": message,
        }
        with self._lock:
            self.recent_events.append(event)
            del self.recent_events[:-50]
        send_windows_toast(f"{label} usage", message)

    def plan_window_snapshot(self):
        now = time.time()
        # Prefer a fresh (<=RATE_LIMITS_STALE_SECONDS) capture; failing
        # that, fall back to whatever official capture exists, however old
        # -- Anthropic's real last-known percentage beats the local ceiling
        # guess even when stale, as long as we account for windows that
        # have since reset (see _official_pct_now below). Only when no
        # capture has EVER been written do we drop to the raw estimate.
        official = load_rate_limits(self.rate_limits_path) or load_rate_limits_any_age(self.rate_limits_path)
        if official:
            rl = official.get("rate_limits") or {}
            five = rl.get("five_hour") or {}
            five_pct = _official_pct_now(five, now)
            if five_pct is not None:
                seven = rl.get("seven_day") or {}
                seven_pct = _official_pct_now(seven, now)

                def resets_min(w):
                    ra = w.get("resets_at")
                    return max(0, int((ra - now) // 60)) if ra else None

                # The statusline only re-captures on session events (a new
                # assistant message, /compact, ...), not on a timer, so the
                # number can lag real usage by however long since the last
                # render. Surfacing that age lets the UI show *why* this can
                # be behind what /usage reports at the exact moment you check
                # it -- and, past RATE_LIMITS_STALE_SECONDS, that it's from a
                # session that's gone idle rather than a live number.
                age_min = max(0, int((now - official.get("captured_at", now)) // 60))
                return {
                    "source": "official",
                    "pct": min(100, round(five_pct)),
                    "resets_in_min": resets_min(five),
                    "week_pct": min(100, round(seven_pct)) if seven_pct is not None else None,
                    "week_resets_in_min": resets_min(seven),
                    "captured_age_min": age_min,
                }
        return {
            "source": "estimate",
            "pct": self.plan_tracker.pct(now),
            "used": self.plan_tracker.window_total,
            "ceiling": self.notify["window_ceiling"],
            "resets_in_min": self.plan_tracker.resets_in_seconds(now) // 60,
        }

    def _save_state(self):
        # Merge onto the originally-loaded sessions rather than replacing
        # them outright — a session outside the active window this poll
        # (gone quiet, or not yet rescanned) must keep its persisted total;
        # otherwise it would silently reset to 0 the next time it's touched.
        sessions = dict(self._raw_state)
        sessions.update({sid: t.state() for sid, t in self.trackers.items()})
        state = {"sessions": sessions, "seen_counts": self._seen_counts,
                  "plan_window": self.plan_tracker.state(),
                  "official_prev": self._official_prev,
                  "official_fired": {k: sorted(v) for k, v in self._official_fired.items()},
                  "official_window_reset_at": self._official_window_reset_at}
        try:
            save_notify_state(self.state_path, state)
        except OSError:
            pass
