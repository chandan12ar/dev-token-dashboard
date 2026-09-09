#!/usr/bin/env python3
"""
Dev Token Dashboard — a fully-local, zero-token-cost dashboard for your Claude Code usage.

It reads the JSONL session logs Claude Code already writes to ~/.claude/projects/
(no API calls, no tokens consumed) and serves a live auto-refreshing dashboard.

Usage:
    python dev_token_dashboard.py            # serves on http://localhost:8787
    python dev_token_dashboard.py --port 9000
    python dev_token_dashboard.py --dir "C:/Users/you/.claude/projects"

Requires: Python 3.8+ (standard library only).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import defaultdict, Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens). Edit freely — these are estimates for the
# "what would this cost on the API" card. Cache write ≈ 1.25x input,
# cache read ≈ 0.1x input.
# ---------------------------------------------------------------------------
PRICING = [
    ("opus",   {"in": 15.0, "out": 75.0}),
    ("sonnet", {"in": 3.0,  "out": 15.0}),
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

CODE_TOOLS_WRITE = {"Write", "Create"}          # tools whose 'content' is new code
CODE_TOOLS_EDIT = {"Edit", "StrEditReplace"}    # tools whose 'new_string' is new code

# tool classification for the exploration-vs-building split
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
              "NotebookRead", "TodoRead", "ToolSearch"}
WRITE_TOOLS = CODE_TOOLS_WRITE | CODE_TOOLS_EDIT | {"MultiEdit", "NotebookEdit"}
CMD_TOOLS = {"Bash", "PowerShell"}   # command runs — the verification proxy for diligence

# Quiet hours for the wellness panel (24h clock, wraps midnight): messages with
# a timestamp >= QUIET_START or < QUIET_END count as late-night activity.
QUIET_START, QUIET_END = 23, 6

CMD_RE = re.compile(r"<command-name>\s*(/?[\w:_-]+)\s*</command-name>")

# prompts that read as corrections of Claude's previous output (heuristic,
# matched at the start of the prompt) — a signal for the fluency report
CORRECT_RE = re.compile(
    r"^(no+[,.! ]|no+$|not (that|what|like)|wrong|nope|incorrect|actually[, ]"
    r"|that'?s (not|wrong)|stop[,.! ]|undo |revert |don'?t |you (missed|forgot|broke))",
    re.IGNORECASE)


def price_for(model: str):
    m = (model or "").lower()
    for key, p in PRICING:
        if key in m:
            return p
    return DEFAULT_PRICE


def count_lines(text) -> int:
    if not isinstance(text, str) or not text:
        return 0
    return text.count("\n") + 1


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _day_stat():
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": 0.0, "loc": 0, "prompts": 0, "prompt_lines": 0,
        "prompt_words": 0, "messages": 0, "human_tokens": 0,
        "reads": 0, "writes": 0, "interruptions": 0, "tool_errors": 0,
        "late_msgs": 0, "corrections": 0,
    }


def _proj_stat():
    return {
        "input": 0, "output": 0, "loc": 0, "prompts": 0,
        "sessions": set(), "cost": 0.0,
        "days": defaultdict(lambda: {"output": 0, "loc": 0}),
        "files": Counter(),
    }


class Stats:
    def __init__(self):
        self.days = defaultdict(_day_stat)
        self.models = defaultdict(lambda: {
            "msgs": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "cost": 0.0,
        })
        self.tools = Counter()
        self.slash = Counter()
        self.projects = defaultdict(_proj_stat)
        self.sessions = {}          # sid -> per-session record
        self.session_titles = {}    # sid -> AI-generated title from the logs
        self.branches = defaultdict(lambda: {"msgs": 0, "output": 0, "loc": 0})
        self.heatmap = defaultdict(int)   # (weekday, hour) -> messages
        self.day_buckets = defaultdict(set)  # day -> set of active 10-min buckets
        self.prompt_words = 0
        self.prompt_chars = 0
        self.longest_prompts = []   # list of (lines, preview, day)
        self.files_touched = Counter()
        self.seen_msg_ids = set()
        self.seen_tool_ids = set()
        self.errors = 0
        self.tool_errors = 0
        self.parse_errors = 0   # log entries we failed to parse (format drift)
        self.permission_modes = Counter()  # auto / default / acceptEdits snapshots
        self.subagents = Counter()         # Agent tool calls, by subagent_type
        self.project_cwd = {}              # project label -> real absolute path (first seen)
        self.last_day = None               # most recent dated entry seen (undated events piggyback on it)

    # -- merge helpers ------------------------------------------------------
    def touch_session(self, sid, ts, project):
        if not sid:
            return None
        s = self.sessions.setdefault(sid, {
            "start": ts, "end": ts, "msgs": 0, "project": project,
            "reads": 0, "writes": 0, "loc": 0, "prompts": 0, "output": 0,
            "bash": 0,
        })
        if ts:
            if not s["start"] or ts < s["start"]:
                s["start"] = ts
            if not s["end"] or ts > s["end"]:
                s["end"] = ts
        s["msgs"] += 1
        return s


def parse_entry(entry: dict, project: str, st: Stats, min_day=None, max_day=None):
    etype = entry.get("type")

    # AI-generated session title lines (written by Claude Code itself)
    if etype == "ai-title":
        sid, title = entry.get("sessionId"), entry.get("aiTitle")
        if sid and title:
            st.session_titles[sid] = title
        return

    # permission-mode snapshots ("auto" / "default" / "acceptEdits") carry no
    # timestamp of their own — they piggyback on the most recently seen dated
    # entry so they still respect the selected date range.
    if etype == "permission-mode":
        day = st.last_day
        if min_day is not None and (not day or day < min_day):
            return
        if max_day is not None and (not day or day > max_day):
            return
        st.permission_modes[entry.get("permissionMode") or "unknown"] += 1
        return

    if etype not in ("user", "assistant"):
        return
    if entry.get("isMeta"):
        return

    ts_raw = entry.get("timestamp") or ""
    day, hour, weekday, ts = "", None, None, None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone()
        day = ts.strftime("%Y-%m-%d")
        hour, weekday = ts.hour, ts.weekday()
    except Exception:
        pass
    if day:
        st.last_day = day

    cwd = entry.get("cwd")
    if cwd and project not in st.project_cwd:
        st.project_cwd[project] = cwd

    # date-window filter (used to build a range-scoped Stats). When no window is
    # set (all-time) undated entries are kept exactly as before.
    if min_day is not None and (not day or day < min_day):
        return
    if max_day is not None and (not day or day > max_day):
        return

    sid = entry.get("sessionId")
    branch = entry.get("gitBranch") or "(none)"
    sess = st.touch_session(sid, ts_raw, project)
    if day:
        st.days[day]["messages"] += 1
    if hour is not None:
        st.heatmap[(weekday, hour)] += 1
        if day:
            # 10-minute activity buckets — the basis for "active time"
            st.day_buckets[day].add(hour * 6 + ts.minute // 10)
            if hour >= QUIET_START or hour < QUIET_END:
                st.days[day]["late_msgs"] += 1

    msg = entry.get("message") or {}
    content = msg.get("content")

    # ------------------------------------------------------------------ user
    if etype == "user":
        # failed tool results ride back on user-type entries
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    st.tool_errors += 1
                    if day:
                        st.days[day]["tool_errors"] += 1
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                text = "\n".join(texts)
        if not text:
            return
        # slash commands
        cmd = CMD_RE.search(text)
        if cmd:
            st.slash[cmd.group(1)] += 1
            return  # command invocations aren't hand-typed prompts
        if text.startswith("<local-command") or text.startswith("<command-"):
            return
        if "[Request interrupted" in text:
            st.errors += 1
            if day:
                st.days[day]["interruptions"] += 1
            return
        lines = count_lines(text)
        words = len(text.split())
        if day and CORRECT_RE.match(text.strip()):
            st.days[day]["corrections"] += 1
        if day:
            st.days[day]["prompts"] += 1
            st.days[day]["prompt_lines"] += lines
            st.days[day]["prompt_words"] += words
            # ~4 chars per token: estimate of tokens the human typed
            st.days[day]["human_tokens"] += max(1, len(text) // 4)
        st.prompt_words += words
        st.prompt_chars += len(text)
        st.projects[project]["prompts"] += 1
        if sess:
            sess["prompts"] += 1
        preview = text.strip().replace("\n", " ")[:120]
        st.longest_prompts.append((lines, words, preview, day))
        st.longest_prompts.sort(key=lambda x: -x[0])
        del st.longest_prompts[40:]   # keep a pool so range-filtering still yields 8
        return

    # -------------------------------------------------------------- assistant
    usage = msg.get("usage") or {}
    model = msg.get("model") or "unknown"
    mid = msg.get("id") or entry.get("uuid")
    dedup_key = (mid, entry.get("requestId"))

    if usage and dedup_key not in st.seen_msg_ids:
        st.seen_msg_ids.add(dedup_key)
        i = usage.get("input_tokens", 0) or 0
        o = usage.get("output_tokens", 0) or 0
        cw = usage.get("cache_creation_input_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        p = price_for(model)
        cost = (i * p["in"] + o * p["out"] + cw * p["in"] * 1.25 + cr * p["in"] * 0.10) / 1_000_000
        if day:
            d = st.days[day]
            d["input"] += i; d["output"] += o
            d["cache_read"] += cr; d["cache_write"] += cw
            d["cost"] += cost
        m = st.models[model]
        m["msgs"] += 1; m["input"] += i; m["output"] += o
        m["cache_read"] += cr; m["cache_write"] += cw; m["cost"] += cost
        pr = st.projects[project]
        pr["input"] += i; pr["output"] += o; pr["cost"] += cost
        if day:
            pr["days"][day]["output"] += o
        if sid:
            pr["sessions"].add(sid)
        if sess:
            sess["output"] += o
        b = st.branches[branch]
        b["msgs"] += 1; b["output"] += o

    # tool calls + lines of code
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tid = block.get("id")
            if tid:
                if tid in st.seen_tool_ids:
                    continue
                st.seen_tool_ids.add(tid)
            name = block.get("name", "?")
            st.tools[name] += 1
            if name in READ_TOOLS:
                if day:
                    st.days[day]["reads"] += 1
                if sess:
                    sess["reads"] += 1
            elif name in WRITE_TOOLS:
                if day:
                    st.days[day]["writes"] += 1
                if sess:
                    sess["writes"] += 1
            elif name in CMD_TOOLS:
                if sess:
                    sess["bash"] += 1
            elif name == "Agent":
                inp0 = block.get("input") or {}
                st.subagents[inp0.get("subagent_type") or "unknown"] += 1
            inp = block.get("input") or {}
            loc = 0
            if name in CODE_TOOLS_WRITE:
                loc = count_lines(inp.get("content"))
            elif name in CODE_TOOLS_EDIT:
                loc = count_lines(inp.get("new_string"))
            elif name == "MultiEdit":
                loc = sum(count_lines(e.get("new_string")) for e in inp.get("edits", [])
                          if isinstance(e, dict))
            elif name == "NotebookEdit":
                loc = count_lines(inp.get("new_source"))
            if loc:
                if day:
                    st.days[day]["loc"] += loc
                st.projects[project]["loc"] += loc
                if day:
                    st.projects[project]["days"][day]["loc"] += loc
                st.branches[branch]["loc"] += loc
                if sess:
                    sess["loc"] += loc
                fp = inp.get("file_path") or inp.get("notebook_path")
                if fp:
                    st.files_touched[os.path.basename(fp)] += loc
                    st.projects[project]["files"][os.path.basename(fp)] += loc


# ---------------------------------------------------------------------------
# Incremental scanner (re-reads only files whose mtime changed)
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self, root):
        self.root = root
        self._cache = {}     # path -> (mtime, size)
        self._entries = {}   # path -> list[dict]
        self._lock = threading.Lock()

    def scan(self):
        found = set()
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, fn)
                found.add(path)
                try:
                    stat = os.stat(path)
                    key = (stat.st_mtime, stat.st_size)
                except OSError:
                    continue
                if self._cache.get(path) == key:
                    continue
                entries = []
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    continue
                self._cache[path] = key
                self._entries[path] = entries
        # drop deleted files
        for path in list(self._entries):
            if path not in found:
                self._entries.pop(path, None)
                self._cache.pop(path, None)

    def build_stats(self, min_day=None, max_day=None) -> Stats:
        with self._lock:
            self.scan()
            st = Stats()
            for path, entries in self._entries.items():
                project = os.path.basename(os.path.dirname(path)) or "unknown"
                # prettify: Claude Code encodes the cwd in the dir name
                project = project.lstrip("-").replace("--", "/").replace("-", "/")
                for e in entries:
                    try:
                        parse_entry(e, project, st, min_day, max_day)
                    except Exception:
                        st.parse_errors += 1
            if st.parse_errors:
                print(f"[warn] {st.parse_errors} log entries could not be parsed "
                      "(log format may have changed - stats may be incomplete)")
            return st


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

    def pct(self):
        if not self.ceiling:
            return 0
        return min(100, int(self.window_total / self.ceiling * 100))

    def resets_in_seconds(self, now_epoch):
        if self.window_start is None:
            return 0
        return max(0, int(self.window_start + WINDOW_SECONDS - now_epoch))

    def state(self):
        return {"window_start": self.window_start, "window_total": self.window_total}


def load_notify_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_notify_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _fmt_k(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def send_windows_toast(title, message):
    """Fire a native Windows toast via the built-in WinRT API through
    powershell.exe — no pip dependency, no module install, matches how
    Restart-Dashboard.ps1 already shells out to PowerShell."""
    if os.name != "nt":
        return
    ps_script = (
        '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
        'ContentType=WindowsRuntime] > $null;'
        '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
        'ContentType=WindowsRuntime] > $null;'
        '$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('
        '[Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
        '$x = $t.GetElementsByTagName("text");'
        '$x.Item(0).AppendChild($t.CreateTextNode($env:DTD_TOAST_TITLE)) > $null;'
        '$x.Item(1).AppendChild($t.CreateTextNode($env:DTD_TOAST_MSG)) > $null;'
        '$n = [Windows.UI.Notifications.ToastNotification]::new($t);'
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
        '"Dev Token Dashboard").Show($n)'
    )
    env = dict(os.environ, DTD_TOAST_TITLE=title, DTD_TOAST_MSG=message)
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", ps_script],
            env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


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
        self.state_path = state_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".notify_state.json")
        self.rate_limits_path = rate_limits_path or RATE_LIMITS_PATH
        saved = load_notify_state(self.state_path)
        self._raw_state = saved.get("sessions", {})
        # last-seen official five_hour/seven_day used_percentage, so a
        # restart doesn't refire milestones already crossed before it -- same
        # "baseline on first sight" reasoning as _seen_counts/baseline_sids.
        self._official_prev = dict(saved.get("official_prev", {}))
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
            prev = self._official_prev.get(kind)
            if prev is not None:
                crossed = milestones_crossed(prev, pct, 100,
                                              milestones=OFFICIAL_MILESTONES[kind])
                if crossed:
                    self._fire_official(kind, label, crossed[-1], pct, window.get("resets_at"))
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
        official = load_rate_limits(self.rate_limits_path)
        if official:
            rl = official.get("rate_limits") or {}
            five = rl.get("five_hour") or {}
            if five.get("used_percentage") is not None:
                seven = rl.get("seven_day") or {}

                def resets_min(w):
                    ra = w.get("resets_at")
                    return max(0, int((ra - time.time()) // 60)) if ra else None

                # The statusline only re-captures on session events (a new
                # assistant message, /compact, ...), not on a timer, so the
                # number can lag real usage by however long since the last
                # render -- up to RATE_LIMITS_STALE_SECONDS. Surfacing that
                # age lets the UI show *why* this can be a couple % behind
                # what /usage reports at the exact moment you check it.
                age_min = max(0, int((time.time() - official.get("captured_at", time.time())) // 60))
                return {
                    "source": "official",
                    "pct": min(100, round(five["used_percentage"])),
                    "resets_in_min": resets_min(five),
                    "week_pct": (min(100, round(seven["used_percentage"]))
                                 if seven.get("used_percentage") is not None else None),
                    "week_resets_in_min": resets_min(seven),
                    "captured_age_min": age_min,
                }
        return {
            "source": "estimate",
            "pct": self.plan_tracker.pct(),
            "used": self.plan_tracker.window_total,
            "ceiling": self.notify["window_ceiling"],
            "resets_in_min": self.plan_tracker.resets_in_seconds(time.time()) // 60,
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
                  "official_prev": self._official_prev}
        try:
            save_notify_state(self.state_path, state)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Task-type classification (heuristic: AI session title first, tool mix as
# fallback) and wellness helpers
# ---------------------------------------------------------------------------

TASK_TYPES = ["feature", "bugfix", "refactor", "docs", "explore", "other"]


def classify_session(title, reads=0, writes=0, loc=0):
    t = " %s " % (title or "").lower()

    def has(*words):
        return any(w in t for w in words)

    if has("fix", " bug", "error", "issue", "crash", "debug", "broken",
           "fail", "troubleshoot", "repair", "not work"):
        return "bugfix"
    if has("refactor", "clean", "rename", "simplif", "reorganiz",
           "restructur", "tidy", "polish", "dedup"):
        return "refactor"
    if has("readme", "document", " docs", " doc ", "changelog",
           "write-up", "writeup", "comment"):
        return "docs"
    if has("add ", "implement", "build", "creat", " new ", "feature",
           "support", "integrat", "set up", "setup", "install", "launch",
           "generat", "redesign", "improve", "update", "upgrade"):
        return "feature"
    if has("explor", "investigat", "understand", "analy", "review",
           "explain", "research", "compare", "look at", "check ",
           "question", "how ", "why ", "what "):
        return "explore"
    # no title signal — fall back to what the session actually did
    if loc > 0 or writes > reads:
        return "feature"
    if reads:
        return "explore"
    return "other"


def longest_focus(buckets) -> int:
    """Longest run of consecutive 10-min activity buckets within a day, in minutes."""
    if not buckets:
        return 0
    b = sorted(buckets)
    best = run = 1
    for i in range(1, len(b)):
        run = run + 1 if b[i] == b[i - 1] + 1 else 1
        best = max(best, run)
    return best * 10


# ---------------------------------------------------------------------------
# Weekly aggregation (for the Weekly Report tab)
# ---------------------------------------------------------------------------

def build_weeks(st: Stats):
    weeks = {}

    def week_of(dt):
        iso = dt.isocalendar()
        y, wn = iso[0], iso[1]
        start = datetime.fromisocalendar(y, wn, 1).strftime("%Y-%m-%d")
        end = datetime.fromisocalendar(y, wn, 7).strftime("%Y-%m-%d")
        return "%d-W%02d" % (y, wn), start, end

    for day, ds in st.days.items():
        try:
            dt = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue
        key, start, end = week_of(dt)
        w = weeks.setdefault(key, {
            "key": key, "start": start, "end": end,
            "input": 0, "output": 0, "loc": 0, "prompts": 0,
            "reads": 0, "writes": 0, "interruptions": 0, "tool_errors": 0,
            "human_tokens": 0, "cost": 0.0, "minutes": 0.0,
            "prompt_words": 0, "corrections": 0, "late_msgs": 0,
            "messages": 0, "active_min": 0,
            "days": [], "sessions": [],
        })
        for f in ("input", "output", "loc", "prompts", "reads", "writes",
                  "interruptions", "tool_errors", "human_tokens",
                  "prompt_words", "corrections", "late_msgs", "messages"):
            w[f] += ds[f]
        w["cost"] += ds["cost"]
        w["active_min"] += len(st.day_buckets.get(day, ())) * 10
        w["days"].append({"day": day, "output": ds["output"], "loc": ds["loc"],
                          "reads": ds["reads"], "writes": ds["writes"]})

    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
        except Exception:
            continue
        key, _, _ = week_of(a)
        if key not in weeks:
            continue
        mins = max(0.0, (b - a).total_seconds()) / 60
        weeks[key]["minutes"] += mins
        title = st.session_titles.get(sid) or "Untitled session"
        weeks[key]["sessions"].append({
            "id": sid[:8], "title": title,
            "project": s["project"], "day": a.strftime("%Y-%m-%d"),
            "min": round(mins), "loc": s["loc"], "reads": s["reads"],
            "writes": s["writes"], "output": s["output"], "prompts": s["prompts"],
            "bash": s["bash"],
            "type": classify_session(title, s["reads"], s["writes"], s["loc"]),
        })

    out = []
    for key in sorted(weeks):
        w = weeks[key]
        w["days"].sort(key=lambda x: x["day"])
        w["sessions"].sort(key=lambda x: x["day"])
        w["minutes"] = round(w["minutes"])
        w["cost"] = round(w["cost"], 2)
        out.append(w)
    return out


def active_streak(days_sorted):
    """Return (current_streak, longest_streak) of consecutive active calendar days.
    current_streak is counted backwards from the most recent active day."""
    from datetime import date, timedelta
    if not days_sorted:
        return 0, 0
    dset = set(days_sorted)
    try:
        d = date.fromisoformat(days_sorted[-1])
    except ValueError:
        return 0, 0
    cur = 0
    while d.isoformat() in dset:
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    prev = None
    for ds in days_sorted:
        try:
            cd = date.fromisoformat(ds)
        except ValueError:
            continue
        run = run + 1 if (prev and (cd - prev).days == 1) else 1
        best = max(best, run)
        prev = cd
    return cur, best


def stats_to_json(st: Stats, days_filter=None) -> dict:
    all_days = sorted(st.days.keys())
    if days_filter:
        all_days = all_days[-days_filter:]
    dayset = set(all_days)

    def tot(key):
        return sum(st.days[d][key] for d in all_days)

    prompts_total = tot("prompts")
    # session count/avg respect the selected range (by the session's start day),
    # so they stay consistent with the ranged token KPIs beside them.
    sess_durs = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
            sday = a.strftime("%Y-%m-%d")
            dur = max(0, (b - a).total_seconds())
        except Exception:
            sday, dur = "", 0
        if days_filter and sday not in dayset:
            continue
        sess_durs.append(dur)
    sessions_count = len(sess_durs)
    avg_session_min = (sum(sess_durs) / sessions_count / 60) if sessions_count else 0

    cache_read = tot("cache_read")
    inp = tot("input")
    cache_eff = cache_read / (cache_read + inp) * 100 if (cache_read + inp) else 0

    projects = []
    for name, p in st.projects.items():
        pdays = sorted(p["days"].keys())
        cwd = st.project_cwd.get(name)
        has_instructions = bool(cwd) and (
            os.path.isfile(os.path.join(cwd, "CLAUDE.md")) or
            os.path.isfile(os.path.join(cwd, "AGENTS.md")))
        projects.append({
            "name": name, "input": p["input"], "output": p["output"],
            "loc": p["loc"], "prompts": p["prompts"],
            "sessions": len(p["sessions"]), "cost": round(p["cost"], 2),
            "days": [{"day": d, **p["days"][d]} for d in pdays],
            "files": p["files"].most_common(8),
            "has_instructions": has_instructions,
        })
    projects.sort(key=lambda x: -(x["input"] + x["output"]))

    models = []
    for name, m in st.models.items():
        models.append({"name": name, **{k: (round(v, 2) if k == "cost" else v)
                                        for k, v in m.items()}})
    models.sort(key=lambda x: -x["output"])

    branches = []
    for name, b in st.branches.items():
        branches.append({"name": name, **b})
    branches.sort(key=lambda x: -x["output"])

    heat = [[st.heatmap.get((wd, h), 0) for h in range(24)] for wd in range(7)]

    sessions_list = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
            day = a.strftime("%Y-%m-%d")
            mins = round(max(0.0, (b - a).total_seconds()) / 60)
        except Exception:
            day, mins = "", 0
        title = st.session_titles.get(sid) or "Untitled session"
        sessions_list.append({
            "id": sid[:8], "title": title,
            "project": s["project"], "day": day, "min": mins,
            "loc": s["loc"], "reads": s["reads"], "writes": s["writes"],
            "output": s["output"], "prompts": s["prompts"], "bash": s["bash"],
            "type": classify_session(title, s["reads"], s["writes"], s["loc"]),
        })
    sessions_list.sort(key=lambda x: x["day"], reverse=True)
    del sessions_list[300:]

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "input": inp, "output": tot("output"),
            "cache_read": cache_read, "cache_write": tot("cache_write"),
            "cost": round(tot("cost"), 2), "loc": tot("loc"),
            "prompts": prompts_total, "messages": tot("messages"),
            "sessions": sessions_count,
            "avg_session_min": round(avg_session_min, 1),
            "cache_efficiency": round(cache_eff, 1),
            "avg_prompt_lines": round(tot("prompt_lines") / prompts_total, 1) if prompts_total else 0,
            "avg_prompt_words": round(tot("prompt_words") / prompts_total, 1) if prompts_total else 0,
            "interruptions": tot("interruptions"),
            "tool_errors": tot("tool_errors"),
            "reads": tot("reads"), "writes": tot("writes"),
            "human_tokens": tot("human_tokens"),
            "leverage": round(tot("output") / tot("human_tokens"), 1) if tot("human_tokens") else 0,
            "parse_errors": st.parse_errors,
            "corrections": tot("corrections"),
            "delegated": sum(st.subagents.values()),
        },
        "days": [{"day": d, **st.days[d], "cost": round(st.days[d]["cost"], 3),
                  "active_min": len(st.day_buckets.get(d, ())) * 10,
                  "focus_max": longest_focus(st.day_buckets.get(d, ()))} for d in all_days],
        "models": models,
        "tools": st.tools.most_common(12),
        "slash": st.slash.most_common(12),
        "projects": projects[:10],
        "branches": branches[:10],
        "heatmap": heat,
        "longest_prompts": [
            {"lines": l, "words": w, "preview": p, "day": d}
            for (l, w, p, d) in st.longest_prompts if d in dayset or not days_filter
        ][:8],
        "top_files": st.files_touched.most_common(8),
        "sessions_list": sessions_list,
        "quiet_hours": [QUIET_START, QUIET_END],
        "permission_modes": st.permission_modes.most_common(),
        "subagents": st.subagents.most_common(10),
        # weekly report data is always computed over all history
        "weeks": build_weeks(st),
    }


# ---------------------------------------------------------------------------
# Optional AI weekly summary (runs `claude -p`; the ONE feature that costs
# tokens, triggered only by an explicit button press in the UI)
# ---------------------------------------------------------------------------

def _run_claude(prompt: str) -> dict:
    import shutil
    import subprocess
    # Resolve the absolute path once. Relying on the child process to re-resolve
    # "claude" on its own PATH (via `cmd /c claude`) is unreliable — it can pick
    # up a stale/broken shim and silently produce no output.
    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "error": "claude CLI not found on PATH."}
    # Call the resolved executable directly, passing the prompt as an argument
    # (avoids stdin-piping quirks). Only .cmd/.bat shims need the cmd wrapper.
    if exe.lower().endswith((".cmd", ".bat")):
        base = ["cmd", "/c", exe]
    else:
        base = [exe]
    try:
        r = subprocess.run(base + ["-p", prompt], capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude -p timed out after 5 minutes."}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    text = (r.stdout or "").strip()
    if text:
        return {"ok": True, "text": text}
    # No summary text — surface the real reason instead of a generic message.
    err = (r.stderr or "").strip()
    return {"ok": False, "error": "claude -p exited %d with no text (%s)"
            % (r.returncode, (err or "no stdout or stderr")[:400])}


def ai_week_summary(week: dict) -> dict:
    sess = week["sessions"]
    bullets = "\n".join(
        "- [%s] %s (%s, %s min, %s LoC)" % (s["project"], s["title"], s["day"], s["min"], s["loc"])
        for s in sess[:50]
    ) or "- (no sessions logged this week)"
    if len(sess) > 50:
        bullets += "\n- ...and %d more sessions" % (len(sess) - 50)
    hours = round(week["minutes"] / 60, 1)
    prompt = (
        "Write a short weekly work update for a developer's standup, covering "
        "%s to %s.\n\nCoding sessions this week:\n%s\n\n"
        "Stats: %s lines of code written, %s prompts, %s output tokens, "
        "~%s hours in sessions, tool mix %s file edits vs %s reads/searches.\n\n"
        "Format: one first-person paragraph of 3-5 sentences, then 3-6 short "
        "bullet points of key accomplishments. Plain markdown, no headers, no fluff."
        % (week["start"], week["end"], bullets, week["loc"], week["prompts"],
           week["output"], hours, week["writes"], week["reads"])
    )
    return _run_claude(prompt)


def ai_reflection(week: dict, question: str) -> dict:
    """Discuss the weekly reflection question with claude -p (button-only)."""
    types = Counter(s.get("type", "other") for s in week["sessions"])
    mix = ", ".join("%d %s" % (n, t) for t, n in types.most_common()) or "no sessions"
    prompt = (
        "You are helping a developer reflect on how they use AI coding tools. "
        "Their local usage dashboard asked them this reflection question:\n\n"
        "\"%s\"\n\n"
        "Context for the week %s to %s: %d sessions (%s), %s lines of code "
        "written via the AI, %s prompts (%s corrections, %s interruptions), "
        "%s file edits vs %s reads/searches, %s tool errors, ~%s active hours, "
        "%s messages during quiet hours (11pm-6am).\n\n"
        "Give a thoughtful, concise reflection (under 180 words): interpret what "
        "the data suggests, name one trade-off worth considering, and end with "
        "one concrete experiment to try next week. Plain text, no headers, "
        "address the developer as 'you'."
        % (question, week["start"], week["end"], len(week["sessions"]), mix,
           week["loc"], week["prompts"], week.get("corrections", 0),
           week["interruptions"], week["writes"], week["reads"],
           week["tool_errors"], round(week.get("active_min", 0) / 60, 1),
           week.get("late_msgs", 0))
    )
    return _run_claude(prompt)


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Dev Token Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<script src="/chart.umd.min.js"></script>
<style>
/* ═══════════════════════════════════════════════════════════════════════
   Tokens — light is the base palette; dark redefines only what changes.
   Every colour has its definition here on bare :root so no value exists
   only inside a media query.
   ═══════════════════════════════════════════════════════════════════════ */
:root{
color-scheme:light dark;
--canvas:#f2f2f5;--canvas-tint:rgba(194,96,61,.05);
--fill:#ffffff;--fill-2:#fafafb;--fill-3:rgba(0,0,0,.04);--fill-4:rgba(0,0,0,.07);
--chrome:rgba(247,247,249,.72);--chrome-side:rgba(243,243,246,.68);
--hair:rgba(0,0,0,.085);--hair-2:rgba(0,0,0,.16);
--txt:#17181c;--txt-2:#4b4d55;--txt-3:#8b8d96;
--acc:#c2603d;--acc-2:#a94e2e;--acc-ink:#fff;
--acc-soft:rgba(194,96,61,.10);--acc-line:rgba(194,96,61,.32);
--sky:#2f7ee0;--violet:#7355ea;--emerald:#0f9367;--amber:#b57d00;
--rose:#d84b64;--teal:#0d8f89;--slate:#a4a9b4;
--grid:rgba(0,0,0,.065);
--sh-1:0 1px 1px rgba(0,0,0,.04),0 1px 3px rgba(0,0,0,.05);
--sh-2:0 1px 2px rgba(0,0,0,.05),0 14px 30px -18px rgba(0,0,0,.28);
--sh-3:0 2px 10px rgba(0,0,0,.10),0 40px 80px -28px rgba(0,0,0,.40);
--r-card:16px;--r-ctl:10px;--r-sheet:24px;--r-pill:999px;
--font:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI Variable Text','Segoe UI',Inter,Roboto,'Helvetica Neue',sans-serif;
--mono:'SF Mono','Cascadia Code',ui-monospace,Consolas,monospace;
}
/* dark — defined twice on purpose: once for the system default, once for the
   explicit toggle, so the toggle wins in both directions */
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--canvas:#0b0c0f;--canvas-tint:rgba(232,145,107,.07);
--fill:#15171c;--fill-2:#111318;--fill-3:rgba(255,255,255,.05);--fill-4:rgba(255,255,255,.09);
--chrome:rgba(15,17,21,.68);--chrome-side:rgba(13,15,19,.62);
--hair:rgba(255,255,255,.09);--hair-2:rgba(255,255,255,.18);
--txt:#eef1f6;--txt-2:#adb3c0;--txt-3:#787f8d;
--acc:#e8916b;--acc-2:#d97757;--acc-ink:#20120c;
--acc-soft:rgba(232,145,107,.14);--acc-line:rgba(232,145,107,.38);
--sky:#5aa4f5;--violet:#a78bfa;--emerald:#34d399;--amber:#fbbf24;
--rose:#fb7185;--teal:#2dd4bf;--slate:#4a515f;
--grid:rgba(255,255,255,.06);
--sh-1:0 1px 1px rgba(0,0,0,.3),0 1px 3px rgba(0,0,0,.3);
--sh-2:0 1px 2px rgba(0,0,0,.4),0 14px 30px -18px rgba(0,0,0,.8);
--sh-3:0 2px 10px rgba(0,0,0,.5),0 40px 80px -28px rgba(0,0,0,.85);
}}
:root[data-theme="dark"]{
--canvas:#0b0c0f;--canvas-tint:rgba(232,145,107,.07);
--fill:#15171c;--fill-2:#111318;--fill-3:rgba(255,255,255,.05);--fill-4:rgba(255,255,255,.09);
--chrome:rgba(15,17,21,.68);--chrome-side:rgba(13,15,19,.62);
--hair:rgba(255,255,255,.09);--hair-2:rgba(255,255,255,.18);
--txt:#eef1f6;--txt-2:#adb3c0;--txt-3:#787f8d;
--acc:#e8916b;--acc-2:#d97757;--acc-ink:#20120c;
--acc-soft:rgba(232,145,107,.14);--acc-line:rgba(232,145,107,.38);
--sky:#5aa4f5;--violet:#a78bfa;--emerald:#34d399;--amber:#fbbf24;
--rose:#fb7185;--teal:#2dd4bf;--slate:#4a515f;
--grid:rgba(255,255,255,.06);
--sh-1:0 1px 1px rgba(0,0,0,.3),0 1px 3px rgba(0,0,0,.3);
--sh-2:0 1px 2px rgba(0,0,0,.4),0 14px 30px -18px rgba(0,0,0,.8);
--sh-3:0 2px 10px rgba(0,0,0,.5),0 40px 80px -28px rgba(0,0,0,.85);
}

*{box-sizing:border-box;margin:0;padding:0}
[hidden]{display:none!important}
body{
background:var(--canvas);color:var(--txt);
font:100%/1.5 var(--font);font-size:.875rem;
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
text-rendering:optimizeLegibility;font-variant-numeric:tabular-nums;
min-height:100vh;
background-image:radial-gradient(1100px 620px at 8% -12%,var(--canvas-tint),transparent 62%);
background-attachment:fixed}
::selection{background:var(--acc-soft)}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--fill-4);border-radius:99px;border:3.5px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--hair-2);background-clip:padding-box;border:3.5px solid transparent}
code{font:12px/1.5 var(--mono);color:var(--txt-2);background:var(--fill-3);
padding:1px 5px;border-radius:5px;letter-spacing:0}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:6px}
button{font:inherit;color:inherit}

/* ── Typography: tracking and leading are size-specific, never one value ── */
h1{font-size:1.55rem;line-height:1.12;letter-spacing:-.024em;font-weight:640}
h2{font-size:1.1rem;line-height:1.22;letter-spacing:-.018em;font-weight:620}
h3{font-size:.875rem;line-height:1.3;letter-spacing:-.008em;font-weight:620}
.eyebrow{font-size:.6875rem;line-height:1.3;letter-spacing:.07em;
text-transform:uppercase;font-weight:660;color:var(--txt-3)}
.num-xl{font-size:1.75rem;line-height:1;letter-spacing:-.03em;font-weight:660}
.num-lg{font-size:1.375rem;line-height:1;letter-spacing:-.024em;font-weight:660}

/* ═══════════════════════════ Shell ═══════════════════════════ */
.shell{display:grid;grid-template-columns:236px minmax(0,1fr);align-items:start}

/* ── Sidebar: the heavier structural material ── */
.side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
padding:20px 14px 16px;gap:18px;
background:var(--chrome-side);
backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);
border-right:1px solid var(--hair);z-index:30}
.ident{display:flex;align-items:center;gap:10px;padding:0 6px}
.mark{width:34px;height:34px;border-radius:10px;flex:none;display:grid;place-items:center;
background:linear-gradient(145deg,var(--acc),var(--acc-2));color:var(--acc-ink);
box-shadow:0 5px 14px -6px var(--acc-line),inset 0 1px 0 rgba(255,255,255,.28)}
.mark svg{width:19px;height:19px}
.ident .nm{font-size:.8125rem;font-weight:620;letter-spacing:-.01em;line-height:1.25}
.ident .sub{font-size:.6875rem;color:var(--txt-3);line-height:1.3;margin-top:1px}

.nav{position:relative;display:flex;flex-direction:column;gap:2px}
.nav .thumb{position:absolute;left:0;top:0;border-radius:var(--r-ctl);
background:var(--fill);box-shadow:var(--sh-1);border:1px solid var(--hair);
pointer-events:none;visibility:hidden;transform-origin:0 0;will-change:transform,height}
.nav button{position:relative;z-index:1;display:flex;align-items:center;gap:10px;
background:none;border:0;cursor:pointer;padding:9px 11px;border-radius:var(--r-ctl);
color:var(--txt-2);font-size:.8125rem;font-weight:520;letter-spacing:-.005em;
text-align:left;transition:color .15s ease}
.nav button svg{width:16px;height:16px;flex:none;color:var(--txt-3);transition:color .15s ease}
.nav button:hover{color:var(--txt)}
.nav button.on{color:var(--txt);font-weight:600}
.nav button.on svg{color:var(--acc)}
.nav button:active{transform:scale(.985)}

.sidefoot{margin-top:auto;display:flex;flex-direction:column;gap:7px;padding:0 4px}
.chip{display:inline-flex;align-items:center;gap:7px;font-size:.6875rem;color:var(--txt-3);
letter-spacing:.005em;padding:5px 9px;border-radius:var(--r-pill);
background:var(--fill-3);border:1px solid transparent;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip svg{width:12px;height:12px;flex:none}
.dot{width:6px;height:6px;border-radius:50%;background:var(--emerald);flex:none;
box-shadow:0 0 0 0 rgba(52,211,153,.5);animation:breathe 2.6s ease-out infinite}
@keyframes breathe{0%{box-shadow:0 0 0 0 rgba(52,211,153,.45)}70%{box-shadow:0 0 0 6px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.themebtn{display:flex;align-items:center;gap:8px;width:100%;margin-top:3px;
background:var(--fill-3);border:1px solid transparent;border-radius:var(--r-ctl);
padding:7px 10px;cursor:pointer;color:var(--txt-2);font-size:.75rem;font-weight:520;
transition:background .15s ease,color .15s ease}
.themebtn:hover{background:var(--fill-4);color:var(--txt)}
.themebtn:active{transform:scale(.98)}
.themebtn svg{width:14px;height:14px}

/* ── Main column & floating toolbar ── */
.main{min-width:0}
.topbar{position:sticky;top:0;z-index:20;
background:var(--chrome);
backdrop-filter:blur(28px) saturate(180%);-webkit-backdrop-filter:blur(28px) saturate(180%)}
.topbar .barin{max-width:1280px;margin:0 auto;padding:16px 30px 14px;
display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap}
.titles{min-width:0}
.titles p{font-size:.75rem;color:var(--txt-3);margin-top:3px;letter-spacing:.002em}
.acts{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
/* scroll edge effect instead of a hard 1px rule */
.edge{height:14px;margin-top:-1px;pointer-events:none;
background:linear-gradient(var(--chrome),transparent);
border-top:1px solid var(--hair);
opacity:var(--sc,0);transition:opacity .2s ease}

.content{max-width:1280px;margin:0 auto;padding:6px 30px 56px}
#alerts:not(:empty){margin:10px 0 4px;display:flex;flex-direction:column;gap:8px}
.warn{display:inline-flex;gap:9px;align-items:center;font-size:.75rem;color:var(--amber);
background:var(--fill);border:1px solid var(--hair);border-left:3px solid var(--amber);
padding:10px 14px;border-radius:var(--r-ctl);box-shadow:var(--sh-1)}
.warn svg{width:15px;height:15px;flex:none}
.warn.notify{color:var(--sky);border-left-color:var(--sky)}
.notifyX{margin-left:auto;background:none;border:0;color:inherit;cursor:pointer;
font-size:1rem;line-height:1;padding:0 2px;flex:none}

/* ═══════════════════════════ Controls ═══════════════════════════ */
.seg{position:relative;display:inline-flex;background:var(--fill-3);
border-radius:var(--r-ctl);padding:3px;gap:0}
.seg .thumb{position:absolute;top:0;left:0;
border-radius:7px;background:var(--fill);box-shadow:var(--sh-1);
pointer-events:none;visibility:hidden;will-change:transform,width;transition:opacity .2s ease}
.seg button{position:relative;z-index:1;background:none;border:0;cursor:pointer;
padding:6px 13px;border-radius:7px;color:var(--txt-3);
font-size:.75rem;font-weight:560;letter-spacing:-.002em;white-space:nowrap;
transition:color .15s ease}
.seg button:hover{color:var(--txt-2)}
.seg button.on{color:var(--txt)}
.seg button:active{transform:scale(.97)}

.btn{display:inline-flex;align-items:center;gap:7px;
background:var(--fill);border:1px solid var(--hair);color:var(--txt-2);
padding:7px 12px;border-radius:var(--r-ctl);cursor:pointer;
font-size:.75rem;font-weight:540;letter-spacing:-.002em;white-space:nowrap;
box-shadow:var(--sh-1);
transition:border-color .15s ease,color .15s ease,transform .1s ease-out,background .15s ease}
.btn svg{width:14px;height:14px;flex:none}
.btn:hover{color:var(--txt);border-color:var(--hair-2)}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.35;cursor:default;transform:none}
.btn.accent{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
.btn.accent:hover{background:var(--acc-soft);border-color:var(--acc)}
.btn.icon{padding:7px 9px}

/* custom-range popover — anchored to its trigger, materialises from it */
.pop-wrap{position:relative}
.pop{position:absolute;top:calc(100% + 8px);right:0;z-index:40;
transform-origin:top right;width:max-content;
background:var(--fill);border:1px solid var(--hair);border-radius:14px;
box-shadow:var(--sh-3);padding:14px;display:none}
.pop.open{display:block}
.pop .row{display:flex;align-items:center;gap:8px}
.pop label{font-size:.6875rem;color:var(--txt-3);display:block;margin-bottom:5px;
letter-spacing:.05em;text-transform:uppercase;font-weight:640}
.pop input[type=date]{background:var(--fill-2);border:1px solid var(--hair);color:var(--txt);
border-radius:8px;padding:7px 9px;font:inherit;font-size:.75rem}
.pop input[type=date]:focus{outline:none;border-color:var(--acc)}
.pop .dash{color:var(--txt-3);align-self:flex-end;padding-bottom:9px}
.pop .foot{display:flex;gap:8px;margin-top:12px;justify-content:flex-end}

/* ═══════════════════════════ Cards ═══════════════════════════ */
.stack{display:flex;flex-direction:column;gap:22px}
.row{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(400px,1fr))}
.row.half{grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
.card{background:var(--fill);border:1px solid var(--hair);border-radius:var(--r-card);
padding:18px 18px 16px;box-shadow:var(--sh-2);min-width:0;
transition:border-color .2s ease}
.card:hover{border-color:var(--hair-2)}
.card.flat{box-shadow:var(--sh-1)}
.chead{display:flex;align-items:center;gap:9px;margin-bottom:15px}
.chead h3{color:var(--txt)}
.chead .gl{width:26px;height:26px;border-radius:8px;flex:none;display:grid;place-items:center;
background:var(--acc-soft);color:var(--acc)}
.chead .gl svg{width:15px;height:15px}
.chead .hint{margin-left:auto;font-size:.6875rem;color:var(--txt-3);font-weight:420;
text-align:right;letter-spacing:.002em}
.cvs{position:relative;height:280px;min-width:0}
.cvs.short{height:196px}
.cvs.mini{height:220px}
canvas{display:block}
.note{color:var(--txt-3);font-size:.6875rem;margin-top:13px;line-height:1.6}

/* entrance — cheap, non-gesture, first render only */
.enter>*{animation:rise .55s cubic-bezier(.18,.72,.2,1) backwards;
animation-delay:calc(var(--i,0)*45ms)}
@keyframes rise{from{opacity:0;transform:translateY(10px) scale(.992)}to{opacity:1;transform:none}}

/* ── Momentum strip ── */
.mom{display:flex;align-items:center;gap:14px 30px;flex-wrap:wrap}
.mblock{display:flex;align-items:center;gap:12px}
.mico{width:40px;height:40px;border-radius:12px;flex:none;display:grid;place-items:center;
background:var(--acc-soft);color:var(--acc)}
.mico svg{width:21px;height:21px}
.mblock .ml{font-size:.6875rem;color:var(--txt-3);margin-top:4px;letter-spacing:.01em}
.vsep{width:1px;align-self:stretch;min-height:38px;background:var(--hair)}
.goals{display:flex;gap:22px;flex-wrap:wrap;margin-left:auto}
.goalw{min-width:158px}
.goalw .gt{display:flex;justify-content:space-between;gap:10px;
font-size:.6875rem;color:var(--txt-3);margin-bottom:6px;letter-spacing:.01em}
.goalw .gt b{color:var(--txt);font-weight:620}
.bar{height:7px;background:var(--fill-4);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:99px;
background:linear-gradient(90deg,var(--acc),var(--acc-2));
transition:width .55s cubic-bezier(.2,.7,.2,1)}
.bar.over>i{background:linear-gradient(90deg,var(--amber),var(--rose))}

/* ── Plan usage (est.) ── */
.planw{display:flex;flex-direction:column;gap:12px}
.plant{display:flex;align-items:baseline;gap:8px;font-weight:650}
.plant .hint{margin-left:0}
.plant .hint.stale{color:var(--amber);font-weight:650}
.planrow{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.plancol{min-width:140px}
.pcs{font-weight:600;font-size:.8125rem}
.pcr{font-size:.6875rem;color:var(--txt-3);margin-top:2px;letter-spacing:.01em}
.planbar{flex:1;min-width:160px;height:8px}
.pcpct{font-size:.75rem;color:var(--txt-3);white-space:nowrap}

/* ── KPI tiles ── */
.kgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:12px}
.kpi{position:relative;overflow:hidden;background:var(--fill);
border:1px solid var(--hair);border-radius:14px;padding:14px 14px 0;
box-shadow:var(--sh-1);
transition:transform .18s cubic-bezier(.2,.7,.2,1),border-color .18s ease,box-shadow .18s ease}
.kpi:hover{transform:translateY(-2px);border-color:var(--hair-2);box-shadow:var(--sh-2)}
.khead{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:11px}
.kpi .ic{width:26px;height:26px;border-radius:8px;flex:none;display:grid;place-items:center;
background:var(--kbg,var(--acc-soft));color:var(--kc,var(--acc))}
.kpi .ic svg{width:14px;height:14px}
.kpi .s{color:var(--txt-3);font-size:.6875rem;margin-top:5px;letter-spacing:.005em}
.kpi .kd{font-size:.6875rem;margin-top:6px;font-weight:560}
.kpi .spkwrap{height:26px;margin:11px -14px 0;opacity:.9}
.spk{width:100%;height:100%;display:block}
.kpi.plain{padding-bottom:14px}
.up{color:var(--emerald)}.down{color:var(--rose)}.flat-d{color:var(--txt-3)}

/* ── Wellness facts ── */
.facts{display:flex;gap:14px 34px;flex-wrap:wrap;margin-bottom:16px}
.facts .fl{font-size:.6875rem;color:var(--txt-3);margin-top:3px;letter-spacing:.01em}

/* ── Tables ── */
.tbl{width:100%;border-collapse:collapse;font-size:.78125rem}
.tbl th,.tbl td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--hair)}
.tbl thead th{color:var(--txt-3);font-weight:640;font-size:.6875rem;
text-transform:uppercase;letter-spacing:.06em;padding-top:0}
.tbl tbody tr:last-child td{border-bottom:none}
.tbl tbody tr{transition:background .12s ease}
.tbl tbody tr:hover{background:var(--fill-3)}
.tbl td.num,.tbl th.num{text-align:right}
.tbl td b{color:var(--txt);font-weight:600}
.tbl td{color:var(--txt-2)}
.scrollx{overflow-x:auto;margin:0 -4px;padding:0 4px}
#tProj tbody tr{cursor:pointer}
#tProj tbody tr:hover{background:var(--acc-soft)}
.rowarrow{color:var(--txt-3);opacity:0;transition:opacity .15s ease,transform .15s ease}
#tProj tbody tr:hover .rowarrow{opacity:1;transform:translateX(2px)}

/* ── Heatmap ── */
.hm{display:grid;grid-template-columns:34px repeat(24,1fr);gap:3px;align-items:center;
font-size:.625rem;color:var(--txt-3);min-width:660px}
.hm .cell{aspect-ratio:1;border-radius:4px;background:var(--fill-3);
transition:outline-color .12s ease,transform .12s cubic-bezier(.2,.7,.2,1);
outline:1.5px solid transparent;outline-offset:1px}
.hm .cell:hover{outline-color:var(--acc);transform:scale(1.18)}
.hm .hh{text-align:center;font-size:.5625rem;letter-spacing:.02em}
.hm .wd{font-size:.625rem;font-weight:560;color:var(--txt-3);letter-spacing:.02em}
.hleg{display:flex;align-items:center;gap:6px;margin-top:14px;
font-size:.6875rem;color:var(--txt-3);justify-content:flex-end}
.hleg i{width:12px;height:12px;border-radius:3px;display:inline-block}

/* ── Prompt list / work list ── */
.plist{list-style:none;display:flex;flex-direction:column;gap:9px}
.plist li{color:var(--txt-2);font-size:.78125rem;line-height:1.55;padding:11px 13px;
background:var(--fill-2);border:1px solid var(--hair);border-radius:11px;
border-left:2.5px solid var(--acc)}
.plist b{color:var(--txt);font-weight:600}
.plist .mt{color:var(--txt-3);font-size:.6875rem;margin-top:4px}
.worklist .grp{margin-bottom:18px}
.worklist .grp:last-child{margin-bottom:0}
.worklist h4{color:var(--acc);font-size:.6875rem;font-weight:660;margin-bottom:8px;
text-transform:uppercase;letter-spacing:.06em}
.worklist ul{list-style:none}
.worklist li{padding:9px 0;border-bottom:1px solid var(--hair);font-size:.8125rem;color:var(--txt-2)}
.worklist li:last-child{border-bottom:none}
.worklist li .meta{color:var(--txt-3);font-size:.6875rem;margin-top:3px}
.empty{color:var(--txt-3);font-size:.8125rem;padding:6px 0}

/* ── Fluency ── */
.flgrid{display:grid;grid-template-columns:minmax(0,300px) minmax(0,1fr);gap:26px;align-items:center}
.flchart{position:relative;height:272px;min-width:0}
.flrow{display:flex;align-items:center;gap:14px;padding:11px 0;
border-bottom:1px solid var(--hair);flex-wrap:wrap}
.flrow:last-child{border-bottom:none}
.flrow .fln{width:104px;flex:none;font-weight:620;font-size:.8125rem;letter-spacing:-.008em}
.flrow .flb{width:116px;flex:none}
.flrow .fls{width:32px;flex:none;text-align:right;font-weight:660;font-size:.9375rem;letter-spacing:-.02em}
.flrow .flt{flex:1;min-width:210px;color:var(--txt-3);font-size:.6875rem;line-height:1.55}
.flrow .flt b{color:var(--txt-2);font-weight:620}

/* ── Reflection / AI output ── */
.reflq{font-size:.9375rem;line-height:1.6;letter-spacing:-.005em;color:var(--txt);
background:var(--acc-soft);border-left:3px solid var(--acc);
padding:14px 16px;border-radius:12px;margin-bottom:14px}
.aiout{white-space:pre-wrap;font-size:.84375rem;line-height:1.72;color:var(--txt-2);
letter-spacing:.002em}
.aiout:empty{display:none}

/* ── Weekly nav ── */
.wknav{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.wknav h2{min-width:210px}

/* ═══════════════════════════ Sheet (project deep-dive) ═══════════════════════════ */
#scrim{position:fixed;inset:0;z-index:90;display:none;
background:rgba(10,11,14,.42);
backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
#sheet{position:fixed;inset:0;z-index:91;display:none;
align-items:flex-start;justify-content:center;padding:44px 16px;
overflow-y:auto;overscroll-behavior:contain}
.sbox{position:relative;width:100%;max-width:880px;
background:var(--fill);border:1px solid var(--hair);border-radius:var(--r-sheet);
box-shadow:var(--sh-3);will-change:transform,opacity;transform-origin:50% 0}
.sgrab{padding:16px 24px 0;cursor:grab;touch-action:none;-webkit-user-select:none;user-select:none}
.sgrab:active{cursor:grabbing}
.sgrab .handle{width:38px;height:4px;border-radius:99px;background:var(--fill-4);margin:0 auto 14px}
.sbox .sbody{padding:0 24px 24px}
#sTitle{font-size:1.1875rem;letter-spacing:-.02em;font-weight:640;line-height:1.2;padding-right:44px}
.ssub{color:var(--txt-3);font-size:.75rem;margin:5px 0 18px;line-height:1.55}
.sbox h3.sh{font-size:.6875rem;font-weight:660;margin:22px 0 10px;color:var(--txt-3);
text-transform:uppercase;letter-spacing:.06em}
#sClose{position:absolute;top:16px;right:18px;width:30px;height:30px;border-radius:9px;
background:var(--fill-3);border:0;color:var(--txt-3);cursor:pointer;
display:grid;place-items:center;transition:background .15s ease,color .15s ease}
#sClose svg{width:15px;height:15px}
#sClose:hover{background:var(--fill-4);color:var(--txt)}
#sClose:active{transform:scale(.92)}

footer{color:var(--txt-3);font-size:.6875rem;margin-top:34px;padding-top:20px;
border-top:1px solid var(--hair);line-height:1.75;max-width:760px}

/* ═══════════════════════════ Responsive ═══════════════════════════ */
@media (max-width:1040px){
.shell{grid-template-columns:1fr}
.side{position:sticky;top:0;height:auto;flex-direction:row;align-items:center;
gap:12px;padding:10px 16px;border-right:0;border-bottom:1px solid var(--hair);
overflow-x:auto;scrollbar-width:none}
.side::-webkit-scrollbar{display:none}
.ident .txt{display:none}
.nav{flex-direction:row;gap:2px}

.nav button{white-space:nowrap}
.sidefoot{margin-top:0;margin-left:auto;flex-direction:row;align-items:center;gap:6px}
.sidefoot .chip.hideable{display:none}
.themebtn{width:auto}.themebtn span{display:none}
.barin,.content{padding-left:20px;padding-right:20px}
}
@media (max-width:760px){
.row,.row.half{grid-template-columns:1fr}
.flgrid{grid-template-columns:1fr}
.flchart{height:236px}
.acts{width:100%;margin-left:0}
.goals{margin-left:0;width:100%}
.barin,.content{padding-left:14px;padding-right:14px}
h1{font-size:1.3125rem}
#sheet{padding:20px 10px}
}

/* ═══════════════════════════ Accessibility ═══════════════════════════ */
@media (prefers-reduced-motion:reduce){
*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
transition-duration:.12s!important;scroll-behavior:auto!important}
.kpi:hover{transform:none}.hm .cell:hover{transform:none}
.enter>*{animation:none}
}
@media (prefers-reduced-transparency:reduce){
.side,.topbar{backdrop-filter:none;-webkit-backdrop-filter:none;
background:var(--fill)}
#scrim{backdrop-filter:none;-webkit-backdrop-filter:none;background:rgba(10,11,14,.62)}
}
@media (prefers-contrast:more){
:root{--hair:rgba(0,0,0,.28);--hair-2:rgba(0,0,0,.5);--txt-3:#5f626b}
:root[data-theme="dark"]{--hair:rgba(255,255,255,.3);--hair-2:rgba(255,255,255,.52);--txt-3:#a4aab6}
.card,.kpi,.btn{border-width:1px;border-color:var(--hair-2)}
.side,.topbar{background:var(--fill);backdrop-filter:none;-webkit-backdrop-filter:none}
}
@media print{.side,.topbar,.acts{display:none}.content{padding:0}.card{break-inside:avoid}}
</style></head><body>
<div class="shell">

<aside class="side">
  <div class="ident">
    <div class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg></div>
    <div class="txt"><div class="nm">Dev Tokens</div><div class="sub">Claude Code analytics</div></div>
  </div>

  <nav class="nav" id="nav" aria-label="Sections">
    <span class="thumb" id="navThumb"></span>
    <button data-s="overview" class="on"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>Overview</button>
    <button data-s="focus"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Time &amp; Focus</button>
    <button data-s="tools"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0 5 5l-10 10a2.8 2.8 0 0 1-4-4z"/><path d="m17 3 4 4"/></svg>Tools &amp; Models</button>
    <button data-s="projects"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m3 7 9-4 9 4-9 4-9-4z"/><path d="m3 12 9 4 9-4M3 17l9 4 9-4"/></svg>Projects</button>
    <button data-s="weekly"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>Weekly Report</button>
  </nav>

  <div class="sidefoot">
    <span class="chip hideable" title="Reads the logs Claude Code already writes locally &mdash; makes no API calls"><span class="dot"></span>Live &middot; 0 tokens</span>
    <span class="chip hideable"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Updated <span id="upd">&hellip;</span></span>
    <button class="themebtn" id="themeBtn" title="Switch appearance"><span id="themeIcon"></span><span id="themeLabel">System</span></button>
  </div>
</aside>

<main class="main">
<header class="topbar">
  <div class="barin">
    <div class="titles"><h1 id="secTitle">Overview</h1><p id="secSub">&hellip;</p></div>
    <div class="acts">

      <div id="actsData" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <div class="seg" id="range" role="group" aria-label="Date range">
          <span class="thumb" id="rangeThumb"></span>
          <button data-d="7">7d</button><button data-d="30" class="on">30d</button>
          <button data-d="90">90d</button><button data-d="0">All</button>
        </div>
        <div class="pop-wrap">
          <button class="btn icon" id="cBtn" title="Custom date range" aria-haspopup="dialog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/></svg></button>
          <div class="pop" id="cPop" role="dialog" aria-label="Custom date range">
            <div class="row">
              <div><label for="cFrom">From</label><input type="date" id="cFrom"></div>
              <span class="dash">&ndash;</span>
              <div><label for="cTo">To</label><input type="date" id="cTo"></div>
            </div>
            <div class="foot"><button class="btn" id="cClear">Clear</button><button class="btn accent" id="cApply">Apply</button></div>
          </div>
        </div>
        <button class="btn icon" id="expCopy" title="Copy a text summary to the clipboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
        <button class="btn icon" id="expCsv" title="Download the daily table as CSV"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M12 18v-6M9 15l3 3 3-3"/></svg></button>
        <button class="btn icon" id="expJson" title="Download the full stats payload as JSON"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg></button>
      </div>

      <div id="actsWeek" class="wknav" style="display:none">
        <button class="btn icon" id="wPrev" title="Previous week"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg></button>
        <button class="btn icon" id="wNext" title="Next week"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></button>
        <button class="btn" id="wCopy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy Markdown</button>
        <button class="btn accent" id="wAI"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>AI summary</button>
      </div>

    </div>
  </div>
  <div class="edge"></div>
</header>

<div class="content">
<div id="alerts"></div>

<!-- ══════════════ Overview ══════════════ -->
<section id="s-overview" class="stack">
  <div class="card flat" id="momCard" style="display:none;--i:0"><div class="mom" id="mom"></div></div>
  <div class="card flat" id="planCard" style="display:none;--i:0"><div id="planUsage"></div></div>
  <div class="kgrid" id="kpis" style="--i:1"></div>
  <div class="card" style="--i:2"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8" rx="1"/><rect x="12" y="6" width="3" height="12" rx="1"/><rect x="17" y="13" width="3" height="5" rx="1"/></svg></span>
    <h3>Daily tokens</h3><span class="hint">input &middot; output &middot; cache read</span></div>
    <div class="cvs"><canvas id="cTokens"></canvas></div></div>
  <div class="card" style="--i:3"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-9 4 18 3-9h4"/></svg></span>
    <h3>You vs Claude</h3><span class="hint">tokens you typed vs Claude produced &middot; log scale</span></div>
    <div class="cvs"><canvas id="cBalance"></canvas></div></div>
  <div class="row" style="--i:4">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg></span>
      <h3>Lines of code / day</h3><span class="hint">written by Claude</span></div>
      <div class="cvs mini"><canvas id="cLoc"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 3 8 21M16 3l-3 18M4 9h16M3 15h16"/></svg></span>
      <h3>Exploration vs building</h3><span class="hint">tool calls / day</span></div>
      <div class="cvs mini"><canvas id="cRW"></canvas></div></div>
  </div>
</section>

<!-- ══════════════ Time & Focus ══════════════ -->
<section id="s-focus" class="stack" hidden>
  <div class="card" style="--i:0"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span>
    <h3>Time &amp; wellness</h3><span class="hint">active time from 10-min buckets &middot; quiet hours 23:00&ndash;06:00</span></div>
    <div class="facts" id="wellFacts"></div>
    <div class="cvs short"><canvas id="cActive"></canvas></div></div>
  <div class="card" style="--i:1"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M3 9h18M9 21V9"/></svg></span>
    <h3>Activity heatmap</h3><span class="hint">messages by weekday &times; hour</span></div>
    <div class="scrollx"><div class="hm" id="heat"></div></div>
    <div class="hleg">Less<i id="lg1"></i><i id="lg2"></i><i id="lg3"></i><i id="lg4"></i>More</div></div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg></span>
      <h3>Task mix / day</h3><span class="hint">sessions classified by title</span></div>
      <div class="cvs mini"><canvas id="cTaskMix"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg></span>
      <h3>Friction / day</h3><span class="hint">interruptions &amp; tool errors</span></div>
      <div class="cvs mini"><canvas id="cFrict"></canvas></div></div>
  </div>
</section>

<!-- ══════════════ Tools & Models ══════════════ -->
<section id="s-tools" class="stack" hidden>
  <div class="row" style="--i:0">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg></span>
      <h3>Model usage</h3><span class="hint">output tokens</span></div>
      <div class="cvs mini"><canvas id="cModels"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg></span>
      <h3>Model breakdown</h3></div>
      <div class="scrollx"><table class="tbl" id="tModels"><thead><tr><th>Model</th><th class="num">Msgs</th><th class="num">In</th><th class="num">Out</th><th class="num">Est. $</th></tr></thead><tbody></tbody></table></div></div>
  </div>
  <div class="row" style="--i:1">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/></svg></span>
      <h3>Autonomy mode</h3><span class="hint">how much you let Claude act without asking</span></div>
      <div class="cvs mini"><canvas id="cAutonomy"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="12" r="3"/><path d="M6 9v6M9 6h4a5 5 0 0 1 5 5"/></svg></span>
      <h3>Delegation</h3><span class="hint">subagents spawned, by type</span></div>
      <div class="cvs mini"><canvas id="cDelegation"></canvas></div></div>
  </div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h7v7M4 20 21 4M4 4l16 16"/></svg></span>
      <h3>Tool usage</h3><span class="hint">calls in range</span></div>
      <div class="cvs"><canvas id="cTools"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6 3 12l6 6M15 6l6 6-6 6"/></svg></span>
      <h3>Slash commands</h3><span class="hint">invocations in range</span></div>
      <div class="cvs"><canvas id="cSlash"></canvas></div></div>
  </div>
</section>

<!-- ══════════════ Projects ══════════════ -->
<section id="s-projects" class="stack" hidden>
  <div class="card" style="--i:0"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 7 9-4 9 4-9 4-9-4z"/><path d="m3 12 9 4 9-4M3 17l9 4 9-4"/></svg></span>
    <h3>Projects</h3><span class="hint">select a row for the deep dive</span></div>
    <div class="scrollx"><table class="tbl" id="tProj"><thead><tr><th>Project</th><th class="num">In</th><th class="num">Out</th><th class="num">LoC</th><th class="num">Est. $</th><th title="Has a CLAUDE.md or AGENTS.md">Instructions</th><th></th></tr></thead><tbody></tbody></table></div></div>
  <div class="row half" style="--i:1">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="8" r="3"/><path d="M6 9v6M18 11c0 4-6 3-6 7"/></svg></span>
      <h3>Git branches</h3></div>
      <div class="scrollx"><table class="tbl" id="tBranch"><thead><tr><th>Branch</th><th class="num">Msgs</th><th class="num">Out</th><th class="num">LoC</th></tr></thead><tbody></tbody></table></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg></span>
      <h3>Most-edited files</h3><span class="hint">by LoC written</span></div>
      <div class="scrollx"><table class="tbl" id="tFiles"><thead><tr><th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table></div></div>
  </div>
  <div class="card" style="--i:2"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span>
    <h3>Longest prompts</h3><span class="hint">your most detailed briefs</span></div>
    <ul class="plist" id="lPrompts"></ul></div>
</section>

<!-- ══════════════ Weekly Report ══════════════ -->
<section id="s-weekly" class="stack" hidden>
  <div class="kgrid" id="wKpis" style="--i:0"></div>
  <div class="card" style="--i:1"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span>
    <h3>What you worked on</h3></div>
    <div class="worklist" id="wWork"></div></div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg></span>
      <h3>Exploration vs building</h3></div>
      <div class="cvs mini"><canvas id="cSplit"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8" rx="1"/><rect x="14" y="6" width="3" height="12" rx="1"/></svg></span>
      <h3>Day by day</h3><span class="hint">output tokens &amp; LoC</span></div>
      <div class="cvs mini"><canvas id="cWeekDays"></canvas></div></div>
  </div>
  <div class="card" style="--i:3"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></svg></span>
    <h3>AI Fluency report</h3><span class="hint">delegation &middot; description &middot; discernment &middot; diligence</span></div>
    <div class="flgrid"><div class="flchart"><canvas id="cFluency"></canvas></div><div id="flList"></div></div>
    <div class="note">Heuristic scores computed locally from your logs, adapted for coding from Anthropic&rsquo;s 4D AI-fluency framework. Formulas in <code>docs/METRICS.md</code>.</div></div>
  <div class="card" style="--i:4"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.8.7 1 1.5 1 2.5h6c0-1 .2-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg></span>
    <h3>Reflection</h3><span class="hint">a question this week&rsquo;s data raises</span></div>
    <div class="reflq" id="reflQ">&hellip;</div>
    <button class="btn accent" id="reflBtn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Discuss with Claude</button>
    <div class="aiout" id="reflOut" style="margin-top:14px"></div>
    <div class="note">Discussing runs <code>claude -p</code> locally &mdash; like the AI summary, it costs tokens only when you press the button.</div></div>
  <div class="card" id="wAIcard" style="display:none;--i:5"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg></span>
    <h3>AI-written summary</h3></div>
    <div class="aiout" id="wAItext"></div>
    <div class="note">Generated locally via <code>claude -p</code> &mdash; the only dashboard feature that consumes tokens, and only when you press the button.</div></div>
</section>

<footer>100% local &middot; reads Claude Code logs from <code id="root">~/.claude/projects</code> &middot; auto-refreshes every 15s.<br>
Costs are estimates at public API pricing (incl. cache read/write rates) &mdash; on a subscription plan the real marginal cost is $0. Edit the <code>PRICING</code> table in the script to tune.</footer>
</div>
</main>
</div>

<div id="scrim"></div>
<div id="sheet" role="dialog" aria-modal="true" aria-labelledby="sTitle"><div class="sbox" id="sBox">
  <div class="sgrab" id="sGrab"><div class="handle"></div>
    <button id="sClose" aria-label="Close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
    <h2 id="sTitle"></h2><div class="ssub" id="sSub"></div>
  </div>
  <div class="sbody">
    <div class="cvs short"><canvas id="cProj"></canvas></div>
    <h3 class="sh">Sessions</h3><ul class="plist" id="sSess"></ul>
    <h3 class="sh">Top files</h3>
    <div class="scrollx"><table class="tbl" id="sFiles"><thead><tr><th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table></div>
  </div>
</div></div>
<script>
/* ══════════════════════════════════════════════════════════════════════
   Dev Token Dashboard — front end
   Motion model: springs, not durations. Every animation starts from the
   value currently on screen, so it can be grabbed and reversed mid-flight.
   ══════════════════════════════════════════════════════════════════════ */

const $=id=>document.getElementById(id);
const fmt=n=>n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':''+n;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtD=s=>{const p=String(s).split('-');return MON[+p[1]-1]+' '+(+p[2]);};
const fmtMin=m=>m>=60?Math.floor(m/60)+'h'+(m%60?' '+(m%60)+'m':''):m+'m';
const TT=['feature','bugfix','refactor','docs','explore','other'];
const cssv=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const hexa=(h,a)=>{const n=parseInt(h.replace('#',''),16);
return 'rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+','+a+')';};
const RM=matchMedia('(prefers-reduced-motion: reduce)');

let days=30,charts={},D=null,wIdx=null,section='overview',customFrom='',customTo='',booted=false;

/* ─────────────────────────── Spring ───────────────────────────
   Apple's two designer parameters: damping ratio and response (seconds).
   Interruptible by construction — `to()` re-targets from the live value
   and carries the current velocity through, so a reversal has no seam. */
class Spring{
  constructor(v,onUpdate,opt){opt=opt||{};
    this.v=v;this.vel=0;this.target=v;this.on=onUpdate;this.raf=0;this.last=0;
    this.zeta=opt.damping===undefined?1:opt.damping;
    this.w=2*Math.PI/(opt.response===undefined?.4:opt.response);}
  jump(t){this.target=this.v=t;this.vel=0;
    if(this.raf){cancelAnimationFrame(this.raf);this.raf=0;}
    this.on(this.v);}
  to(t,vel){
    if(RM.matches){this.jump(t);this.done&&this.done();return;}
    this.target=t;if(vel!==undefined)this.vel=vel;this.run();}
  run(){if(this.raf)return;this.last=performance.now();
    const step=now=>{
      const dt=Math.min(.05,(now-this.last)/1000);this.last=now;
      const n=Math.max(1,Math.ceil(dt/.004)),h=dt/n;
      for(let i=0;i<n;i++){
        const f=-this.w*this.w*(this.v-this.target)-2*this.zeta*this.w*this.vel;
        this.vel+=f*h;this.v+=this.vel*h;}
      this.on(this.v);
      if(Math.abs(this.v-this.target)<.004&&Math.abs(this.vel)<.04){
        this.v=this.target;this.vel=0;this.on(this.v);this.raf=0;
        this.done&&this.done();return;}
      this.raf=requestAnimationFrame(step);};
    this.raf=requestAnimationFrame(step);}
}
/* Apple's momentum projection (exponential decay), not v²/2a */
const projectTo=(v,rate)=>(v/1000)*(rate||.998)/(1-(rate||.998));
/* progressive resistance past a boundary instead of a hard stop */
const rubber=(over,dim,c)=>{c=c||.55;return (over*dim*c)/(dim+c*Math.abs(over));};

/* ─── Selection indicator: 4 independent springs (never one 2D spring) ─── */
function Indicator(thumb){
  let x=0,y=0,w=0,h=0,seeded=false;
  const apply=()=>{thumb.style.transform='translate3d('+x+'px,'+y+'px,0)';
    thumb.style.width=w+'px';thumb.style.height=h+'px';
    thumb.style.visibility='visible';};
  const o={damping:1,response:.36};
  const sx=new Spring(0,v=>{x=v;apply();},o), sy=new Spring(0,v=>{y=v;apply();},o),
        sw=new Spring(0,v=>{w=v;apply();},o), sh=new Spring(0,v=>{h=v;apply();},o);
  return{move(el,instant){if(!el||!el.offsetParent&&!el.offsetWidth)return;
    const t=[el.offsetLeft,el.offsetTop,el.offsetWidth,el.offsetHeight];
    if(!seeded||instant){seeded=true;sx.jump(t[0]);sy.jump(t[1]);sw.jump(t[2]);sh.jump(t[3]);}
    else{sx.to(t[0]);sy.to(t[1]);sw.to(t[2]);sh.to(t[3]);}}};
}
const navInd=Indicator($('navThumb')),rangeInd=Indicator($('rangeThumb'));

/* ─────────────────────────── Appearance ─────────────────────────── */
const SUN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
const AUTO='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor" stroke="none"/></svg>';
const THEMES=[['system','System',AUTO],['light','Light',SUN],['dark','Dark',MOON]];
let themeIx=0;
function applyTheme(persist){
  const t=THEMES[themeIx];
  if(t[0]==='system')document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme',t[0]);
  $('themeIcon').innerHTML=t[2];$('themeLabel').textContent=t[1];
  if(persist){try{localStorage.setItem('dtd-theme',t[0]);}catch(e){}}
  themeCharts();if(D)render();paintLegend();
}
try{const sv=localStorage.getItem('dtd-theme');
  const i=THEMES.findIndex(t=>t[0]===sv);if(i>=0)themeIx=i;}catch(e){}
$('themeBtn').onclick=()=>{themeIx=(themeIx+1)%THEMES.length;applyTheme(true);};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{
  if(THEMES[themeIx][0]==='system'){themeCharts();if(D)render();paintLegend();}});

/* ─────────────────────────── Chart.js theming ─────────────────────────── */
let C={},PIE=[],GRID={},XGRID={},ANIM={duration:0};
function themeCharts(){
  C={sky:cssv('--sky'),emerald:cssv('--emerald'),violet:cssv('--violet'),amber:cssv('--amber'),
     rose:cssv('--rose'),teal:cssv('--teal'),coral:cssv('--acc'),slate:cssv('--slate'),
     txt:cssv('--txt'),txt2:cssv('--txt-2'),txt3:cssv('--txt-3'),
     grid:cssv('--grid'),fill:cssv('--fill'),hair:cssv('--hair')};
  PIE=[C.coral,C.sky,C.violet,C.emerald,C.amber,C.teal,C.rose];
  GRID={grid:{color:C.grid,drawTicks:false},border:{display:false},ticks:{padding:8}};
  XGRID={grid:{display:false},border:{display:false},
         ticks:{padding:6,maxRotation:0,autoSkip:true,maxTicksLimit:12}};
  const F=cssv('--font');
  Chart.defaults.font.family=F;Chart.defaults.font.size=11;
  Chart.defaults.font.weight=500;
  Chart.defaults.color=C.txt3;Chart.defaults.borderColor=C.grid;
  const L=Chart.defaults.plugins.legend.labels;
  L.usePointStyle=true;L.boxWidth=7;L.boxHeight=7;L.padding=16;L.color=C.txt3;
  const T=Chart.defaults.plugins.tooltip;
  T.backgroundColor=C.fill;T.borderColor=C.hair;T.borderWidth=1;T.padding=11;
  T.cornerRadius=11;T.titleColor=C.txt;T.bodyColor=C.txt2;T.usePointStyle=true;
  T.boxPadding=5;T.displayColors=true;T.titleFont={weight:'600',size:12};
  Chart.defaults.elements.bar.borderRadius=5;Chart.defaults.elements.bar.borderSkipped=false;
  Chart.defaults.elements.point.radius=0;Chart.defaults.elements.point.hoverRadius=5;
  Chart.defaults.elements.point.hitRadius=14;
  Chart.defaults.elements.line.tension=.36;Chart.defaults.elements.line.borderWidth=2;
  Chart.defaults.maintainAspectRatio=false;
  ANIM=RM.matches?{duration:1}:{duration:520,easing:'easeOutQuart'};
}
function mk(id,cfg){const el=$(id);if(!el)return;
  if(charts[id]){charts[id].destroy();delete charts[id];}
  cfg.options=cfg.options||{};
  if(!('animation' in cfg.options))cfg.options.animation=ANIM;
  const ch=charts[id]=new Chart(el,cfg);
  /* Chart.js's construction-time auto-draw can silently no-op (its internal
     animate/update pipeline occasionally never fires the first paint) —
     force one explicit synchronous draw so the chart is never left blank. */
  ch.draw();}
function areaFill(hex){return ctx=>{const ch=ctx.chart,a=ch.chartArea;
  if(!a)return hexa(hex,.14);
  const g=ch.ctx.createLinearGradient(0,a.top,0,a.bottom);
  g.addColorStop(0,hexa(hex,.34));g.addColorStop(1,hexa(hex,0));return g;};}
function paintLegend(){const a=cssv('--acc');
  [['lg1',.14],['lg2',.38],['lg3',.64],['lg4',.92]].forEach(p=>{
    const e=$(p[0]);if(e)e.style.background=hexa(a,p[1]);});}

/* ─────────────────────────── Sparklines ─────────────────────────── */
let spkSeq=0;
function spark(vals,color){
  if(!vals||vals.length<2)return'';
  const w=100,h=26,mx=Math.max.apply(null,vals),mn=Math.min.apply(null,vals),r=(mx-mn)||1;
  const pts=vals.map((v,i)=>[i/(vals.length-1)*w,h-2-((v-mn)/r)*(h-5)]);
  const d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  const id='spk'+(++spkSeq);
  return '<div class="spkwrap"><svg class="spk" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" aria-hidden="true">'+
  '<defs><linearGradient id="'+id+'" x1="0" y1="0" x2="0" y2="1">'+
  '<stop offset="0" stop-color="'+color+'" stop-opacity=".26"/>'+
  '<stop offset="1" stop-color="'+color+'" stop-opacity="0"/></linearGradient></defs>'+
  '<path d="'+d+' L'+w+' '+h+' L0 '+h+' Z" fill="url(#'+id+')"/>'+
  '<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="1.5" stroke-linecap="round" '+
  'stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg></div>';
}

/* ─────────────────────────── Icons ─────────────────────────── */
const IC={in:'<path d="M12 3v13m0 0 4-4m-4 4-4-4M4 21h16"/>',
out:'<path d="M12 21V8m0 0 4 4m-4-4-4 4M4 3h16"/>',
cache:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
cost:'<path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
code:'<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
prompt:'<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
session:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
alert:'<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
user:'<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
lever:'<path d="M23 6l-9.5 9.5-5-5L1 18"/><path d="M17 6h6v6"/>',
flame:'<path d="M12 2s5 4.5 5 9a5 5 0 0 1-10 0c0-1.2.4-2.3.4-2.3S5 10 5 13a7 7 0 0 0 14 0c0-5.5-7-11-7-11z"/>',
cal:'<rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/>',
fix:'<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>'};
const ic=k=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+IC[k]+'</svg>';

/* ─────────────────────────── Navigation ─────────────────────────── */
const META={
  overview:['Overview','The headline numbers and how they move.'],
  focus:['Time &amp; Focus','When you work, how long, and what it costs you.'],
  tools:['Tools &amp; Models','Which models answered and which tools they reached for.'],
  projects:['Projects','Where the work landed — by repo, branch and file.'],
  weekly:['Weekly Report','Always full history — the range filter does not apply here.']};

$('nav').addEventListener('click',e=>{
  const b=e.target.closest('button[data-s]');if(!b)return;setSection(b.dataset.s);});

function setSection(s){
  section=s;
  [...$('nav').querySelectorAll('button[data-s]')].forEach(b=>{
    const on=b.dataset.s===s;b.classList.toggle('on',on);
    b.setAttribute('aria-current',on?'page':'false');
    if(on)navInd.move(b);});
  ['overview','focus','tools','projects','weekly'].forEach(k=>{
    const el=$('s-'+k);el.hidden=k!==s;
    if(k===s){el.classList.remove('enter');void el.offsetWidth;el.classList.add('enter');
      setTimeout(()=>el.classList.remove('enter'),900);}});
  $('actsData').style.display=s==='weekly'?'none':'flex';
  $('actsWeek').style.display=s==='weekly'?'flex':'none';
  updateChrome();
  if(D)render();
  if(s!=='weekly')requestAnimationFrame(()=>rangeInd.move($('range').querySelector('.on'),true));
}
function rangeText(){
  if(customFrom||customTo)return (customFrom||'the start')+' → '+(customTo||'today');
  return days?('Last '+days+' days'):'All time';}
function updateChrome(){
  const m=META[section];$('secTitle').innerHTML=m[0];
  let sub=m[1];
  if(section!=='weekly')sub=rangeText()+' · '+sub;
  else if(D&&D.weeks&&D.weeks.length&&wIdx!==null&&D.weeks[wIdx])
    sub='Week of '+fmtD(D.weeks[wIdx].start)+' – '+fmtD(D.weeks[wIdx].end)+' · '+sub;
  $('secSub').innerHTML=sub;}

/* ─────────────────────────── Range controls ─────────────────────────── */
$('range').addEventListener('click',e=>{
  const b=e.target.closest('button[data-d]');if(!b)return;
  days=+b.dataset.d;customFrom='';customTo='';
  $('cFrom').value='';$('cTo').value='';
  [...$('range').querySelectorAll('button')].forEach(x=>x.classList.remove('on'));
  b.classList.add('on');$('rangeThumb').style.opacity=1;
  rangeInd.move(b);updateChrome();load();});

/* custom-range popover — scales out of its own trigger, not the page centre */
const popS=new Spring(0,v=>{const p=$('cPop');
  p.style.opacity=v;p.style.transform='scale('+(.92+.08*v)+') translateY('+((1-v)*-5)+'px)';},
  {damping:1,response:.3});
let popOpen=false;
popS.done=()=>{if(!popOpen&&popS.target===0)$('cPop').classList.remove('open');};
function togglePop(open){
  popOpen=open;const p=$('cPop');
  if(open){p.classList.add('open');popS.to(1);}else{popS.to(0);
    if(RM.matches)p.classList.remove('open');}}
$('cBtn').onclick=e=>{e.stopPropagation();togglePop(!popOpen);};
$('cPop').onclick=e=>e.stopPropagation();
document.addEventListener('click',()=>{if(popOpen)togglePop(false);});
$('cApply').onclick=()=>{
  const f=$('cFrom').value,t=$('cTo').value;if(!f&&!t)return;
  customFrom=f;customTo=t;
  [...$('range').querySelectorAll('button')].forEach(x=>x.classList.remove('on'));
  $('rangeThumb').style.opacity=0;          // no segment owns the selection now
  togglePop(false);updateChrome();load();};
$('cClear').onclick=()=>{
  customFrom='';customTo='';$('cFrom').value='';$('cTo').value='';
  const b=$('range').querySelector('[data-d="'+days+'"]');
  $('rangeThumb').style.opacity=1;
  if(b){b.classList.add('on');rangeInd.move(b);}
  togglePop(false);updateChrome();load();};

/* ─────────────────────────── Scroll edge ─────────────────────────── */
let edgeTick=false;
addEventListener('scroll',()=>{if(edgeTick)return;edgeTick=true;
  requestAnimationFrame(()=>{edgeTick=false;
    document.documentElement.style.setProperty('--sc',Math.min(1,scrollY/26).toFixed(3));});},
  {passive:true});
addEventListener('resize',()=>{
  navInd.move($('nav').querySelector('.on'),true);
  const rb=$('range').querySelector('.on');if(rb)rangeInd.move(rb,true);});

/* ─────────────────────────── Data ─────────────────────────── */
async function load(){
  const qs=(customFrom||customTo)
    ?('from='+encodeURIComponent(customFrom)+'&to='+encodeURIComponent(customTo))
    :('days='+days);
  let data;
  try{
    const r=await fetch('/api/stats?'+qs);
    if(!r.ok)throw new Error('HTTP '+r.status);
    data=await r.json();
  }catch(err){
    console.error('Failed to load dashboard data:',err);
    let cw=$('connwarn');
    if(!cw){cw=document.createElement('div');cw.id='connwarn';cw.className='warn';
      $('alerts').appendChild(cw);}
    cw.innerHTML=ic('alert')+'<span>Can’t reach the dashboard server — retrying every 15s. '+
      (D?'Showing the last data loaded.':'')+'</span>';
    return;   // keep whatever was last on screen; setInterval(load,...) will retry
  }
  const cw=$('connwarn');if(cw)cw.remove();
  D=data;
  $('upd').textContent=D.generated;
  $('root').textContent=D.root||'~/.claude/projects';
  let pw=$('parsewarn');
  if(D.totals.parse_errors>0){
    if(!pw){pw=document.createElement('div');pw.id='parsewarn';pw.className='warn';
      $('alerts').appendChild(pw);}
    pw.innerHTML=ic('alert')+'<span>'+
      D.totals.parse_errors.toLocaleString()+
      ' log entries could not be parsed — the log format may have changed; stats may be incomplete.</span>';
  }else if(pw){pw.remove();}
  if(D.notifications&&D.notifications.length){
    const seen=window.__notifySeen||(window.__notifySeen=new Set());
    D.notifications.forEach(n=>{
      if(seen.has(n.id))return;
      seen.add(n.id);
      const el=document.createElement('div');
      el.className='warn notify';
      el.innerHTML=ic('alert')+'<span>'+esc(n.project)+' — '+esc(n.title)+': '+
        esc(n.message)+'</span><button class="notifyX" aria-label="Dismiss">&times;</button>';
      el.querySelector('.notifyX').onclick=()=>el.remove();
      $('alerts').appendChild(el);
      setTimeout(()=>el.remove(),20000);
    });
  }
  if(wIdx===null&&D.weeks.length)wIdx=D.weeks.length-1;
  if(wIdx!==null&&wIdx>=D.weeks.length)wIdx=D.weeks.length-1;
  updateChrome();render();
  if(!booted){booted=true;
    requestAnimationFrame(()=>{navInd.move($('nav').querySelector('.on'),true);
      rangeInd.move($('range').querySelector('.on'),true);});}
}
function render(){
  ({overview:renderOverview,focus:renderFocus,tools:renderTools,
    projects:renderProjects,weekly:renderWeek}[section])();}

function delta(cur,prev){
  if(prev==null||prev===0||!isFinite(cur/prev))return'';
  const pc=Math.round((cur-prev)/prev*100);
  if(pc===0)return'<span class="flat-d">no change vs last period</span>';
  return '<span class="'+(pc>0?'up':'down')+'">'+(pc>0?'▲':'▼')+' '+
    Math.abs(pc)+'% vs last period</span>';}

/* ─────────────────────────── Overview ─────────────────────────── */
function renderMomentum(){
  const card=$('momCard');
  if(D.streak===undefined){card.style.display='none';return;}
  card.style.display='';
  const g=D.goals||{},today=D.today||{};
  let h='<div class="mblock"><div class="mico">'+ic('flame')+'</div><div>'+
    '<div class="num-xl">'+D.streak+'</div><div class="ml">day streak · best '+D.best_streak+'</div></div></div>'+
    '<div class="vsep"></div>'+
    '<div class="mblock"><div class="mico">'+ic('cal')+'</div><div>'+
    '<div class="num-xl">'+D.active_days+'</div><div class="ml">active days all-time</div></div></div>';
  const bars=[];
  const bar=(label,val,goal)=>{if(!goal)return;
    const pc=Math.min(100,Math.round(val/goal*100)),over=val>goal;
    bars.push('<div class="goalw"><div class="gt"><span>'+label+'</span><b>'+fmt(val)+' / '+fmt(goal)+
      '</b></div><div class="bar'+(over?' over':'')+'"><i style="width:'+pc+'%"></i></div></div>');};
  bar("Today's LoC",today.loc||0,g.daily_loc);
  bar("Today's output",today.tokens||0,g.daily_tokens);
  bar('This week output',D.week_tokens||0,g.weekly_tokens);
  if(bars.length)h+='<div class="goals">'+bars.join('')+'</div>';
  $('mom').innerHTML=h;}

function renderPlanUsage(){
  const card=$('planCard'),pw=D.plan_window;
  const active=pw&&(pw.source==='official'||pw.ceiling);
  if(!active){card.style.display='none';return;}
  card.style.display='';
  const isOfficial=pw.source==='official';
  const barRow=(title,pct,mins)=>{
    const p=Math.min(100,pct),over=pct>=100,hasMins=mins!=null;
    const rh=hasMins?Math.floor(mins/60):0,rm=hasMins?mins%60:0;
    return '<div class="planrow"><div class="plancol"><div class="pcs">'+title+'</div>'+
      (hasMins?'<div class="pcr">Resets in '+rh+'h '+rm+'m</div>':'')+'</div>'+
      '<div class="bar planbar'+(over?' over':'')+'"><i style="width:'+p+'%"></i></div>'+
      '<div class="pcpct">'+pct+'% used</div></div>';};
  let rows=barRow('Current session',pw.pct,pw.resets_in_min);
  if(isOfficial&&pw.week_pct!=null)rows+=barRow('This week',pw.week_pct,pw.week_resets_in_min);
  // The statusline only re-captures on Claude Code activity (see statusline.js),
  // so a gap here means no session on this machine has made an API call
  // recently -- the number shown is real, just not necessarily current.
  // Above STALE_AGE_MIN, say so loudly rather than a quiet gray hint.
  const STALE_AGE_MIN=2;
  let age='';
  if(isOfficial&&pw.captured_age_min!=null){
    const stale=pw.captured_age_min>=STALE_AGE_MIN;
    age=' <span class="hint'+(stale?' stale':'')+'" title="Updates only when a Claude Code session on this machine is active. It can lag the live /usage number during idle stretches.">'+
      (stale?'⚠ last updated '+pw.captured_age_min+'m ago, may be behind'
            :(pw.captured_age_min<1?'as of just now':'as of '+pw.captured_age_min+'m ago'))+
      '</span>';
  }
  $('planUsage').innerHTML=
    '<div class="planw"><div class="plant">Plan usage limits<span class="hint">'+
    (isOfficial?'(live)':'(estimate)')+'</span>'+age+'</div>'+rows+'</div>';}

function renderOverview(){
  const d=D,t=d.totals;
  renderMomentum();
  renderPlanUsage();
  const S=k=>d.days.map(x=>x[k]||0);
  const kpis=[
    ['Input tokens',fmt(t.input),'sent to Claude','in',C.sky,t.input,'input',S('input')],
    ['Output tokens',fmt(t.output),'generated by Claude','out',C.emerald,t.output,'output',S('output')],
    ['Cache read',fmt(t.cache_read),t.cache_efficiency+'% cache hit rate','cache',C.teal,t.cache_read,'cache_read',S('cache_read')],
    ['Est. API cost','$'+t.cost,'$0 on a subscription plan','cost',C.amber,t.cost,'cost',S('cost')],
    ['Lines of code',fmt(t.loc),'written by Claude','code',C.violet,t.loc,'loc',S('loc')],
    ['Prompts',fmt(t.prompts),t.avg_prompt_lines+' lines avg','prompt',C.coral,t.prompts,'prompts',S('prompts')],
    ['Sessions',t.sessions,t.avg_session_min+' min avg','session',C.sky,t.sessions,'sessions',null],
    ['Friction',t.interruptions,t.tool_errors+' tool errors','alert',C.rose,null,null,
      d.days.map(x=>(x.interruptions||0)+(x.tool_errors||0))],
    ['Corrections',fmt(t.corrections),(t.prompts?Math.round(t.corrections/t.prompts*100):0)+'% of prompts','fix',C.rose,t.corrections,'corrections',S('corrections')],
    ['Your tokens',fmt(t.human_tokens),'typed in prompts (est.)','user',C.amber,t.human_tokens,'human_tokens',S('human_tokens')],
    ['Leverage',t.leverage+'×','Claude tokens per typed token','lever',C.emerald,null,null,null]];
  $('kpis').innerHTML=kpis.map(k=>{
    const dh=(k[6]&&D.prev)?delta(k[5],D.prev[k[6]]):'';
    const sp=k[7]&&k[7].some(v=>v>0)?spark(k[7],k[4]):'';
    return '<div class="kpi'+(sp?'':' plain')+'" style="--kc:'+k[4]+';--kbg:'+hexa(k[4],.13)+'">'+
      '<div class="khead"><span class="eyebrow">'+k[0]+'</span><span class="ic">'+ic(k[3])+'</span></div>'+
      '<div class="num-lg">'+k[1]+'</div>'+
      (dh?'<div class="kd">'+dh+'</div>':'')+
      '<div class="s">'+k[2]+'</div>'+sp+'</div>';}).join('');

  const labels=d.days.map(x=>x.day.slice(5));
  mk('cTokens',{type:'bar',data:{labels,datasets:[
    {label:'Input',data:S('input'),backgroundColor:C.sky},
    {label:'Output',data:S('output'),backgroundColor:C.emerald},
    {label:'Cache read',data:S('cache_read'),backgroundColor:C.slate}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cBalance',{type:'line',data:{labels,datasets:[
    {label:'You (typed)',data:S('human_tokens'),borderColor:C.amber,backgroundColor:areaFill(C.amber),fill:true},
    {label:'Claude (output)',data:S('output'),borderColor:C.coral,backgroundColor:areaFill(C.coral),fill:true}]},
    options:{scales:{x:XGRID,y:{type:'logarithmic',...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cLoc',{type:'line',data:{labels,datasets:[{label:'LoC',data:S('loc'),
    borderColor:C.violet,backgroundColor:areaFill(C.violet),fill:true}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false}}}});
  mk('cRW',{type:'bar',data:{labels,datasets:[
    {label:'Exploration (reads/searches)',data:S('reads'),backgroundColor:C.sky},
    {label:'Building (file edits)',data:S('writes'),backgroundColor:C.emerald}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
}

/* ─────────────────────────── Time & Focus ─────────────────────────── */
function renderFocus(){
  const d=D,labels=d.days.map(x=>x.day.slice(5));
  const sum=k=>d.days.reduce((a,x)=>a+(x[k]||0),0);
  const actTot=sum('active_min'),msgTot=sum('messages'),lateTot=sum('late_msgs');
  const focusMax=d.days.reduce((a,x)=>Math.max(a,x.focus_max||0),0);
  const wknd=d.days.reduce((a,x)=>{const g=new Date(x.day+'T12:00:00').getDay();
    return a+((g===0||g===6)?x.messages:0);},0);
  const actDays=d.days.filter(x=>x.messages>0).length;
  const wf=[[fmtMin(actTot),'active time in range'],
    [actDays?fmtMin(Math.round(actTot/actDays)):'0m','avg per active day'],
    [fmtMin(focusMax),focusMax>=180?'longest focus block — take breaks':'longest focus block'],
    [(msgTot?Math.round(lateTot/msgTot*100):0)+'%','in quiet hours (23–06)'],
    [(msgTot?Math.round(wknd/msgTot*100):0)+'%','on weekends']];
  $('wellFacts').innerHTML=wf.map(f=>
    '<div><div class="num-lg">'+f[0]+'</div><div class="fl">'+f[1]+'</div></div>').join('');
  mk('cActive',{type:'bar',data:{labels,datasets:[{label:'Active minutes',
    data:d.days.map(x=>x.active_min||0),
    backgroundColor:d.days.map(x=>((x.late_msgs||0)>x.messages*.25&&x.messages)?C.rose:C.teal)}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false},tooltip:{callbacks:{
      label:c=>fmtMin(c.parsed.y)+' active'+
        ((d.days[c.dataIndex].late_msgs||0)>d.days[c.dataIndex].messages*.25?' · heavy quiet-hours use':'')}}}}});

  const byDay={};d.days.forEach(x=>byDay[x.day]=Object.fromEntries(TT.map(t=>[t,0])));
  (D.sessions_list||[]).forEach(s=>{if(byDay[s.day]&&s.type)byDay[s.day][s.type]++;});
  const TC={feature:C.emerald,bugfix:C.rose,refactor:C.violet,docs:C.amber,explore:C.sky,other:C.slate};
  mk('cTaskMix',{type:'bar',data:{labels,datasets:TT.map(t=>
    ({label:t,data:d.days.map(x=>byDay[x.day][t]),backgroundColor:TC[t]}))},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID,ticks:{precision:0}}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cFrict',{type:'bar',data:{labels,datasets:[
    {label:'Interruptions',data:d.days.map(x=>x.interruptions),backgroundColor:C.rose},
    {label:'Tool errors',data:d.days.map(x=>x.tool_errors),backgroundColor:C.amber}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});

  const wd=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const max=Math.max(1,...d.heatmap.flat()),acc=cssv('--acc'),base=cssv('--fill-3');
  let hm='<div></div>'+[...Array(24).keys()].map(h=>'<div class="hh">'+(h%2?'':h)+'</div>').join('');
  d.heatmap.forEach((row,i)=>{hm+='<div class="wd">'+wd[i]+'</div>'+row.map((v,h)=>
    '<div class="cell" title="'+wd[i]+' '+h+':00 — '+v+' message'+(v===1?'':'s')+
    '" style="background:'+(v?hexa(acc,(.14+.78*v/max).toFixed(3)):base)+'"></div>').join('');});
  $('heat').innerHTML=hm;paintLegend();
}

/* ─────────────────────────── Tools & Models ─────────────────────────── */
function renderTools(){
  const d=D;
  mk('cModels',{type:'doughnut',data:{labels:d.models.map(m=>m.name),
    datasets:[{data:d.models.map(m=>m.output),backgroundColor:PIE,
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  const AUTO_LBL={auto:'Auto (full autonomy)',default:'Default (asks each time)',
    acceptEdits:'Accept edits (asks before commands)',unknown:'Unknown'};
  const AUTO_CLR={auto:C.emerald,default:C.sky,acceptEdits:C.violet,unknown:C.slate};
  const pm=d.permission_modes||[];
  mk('cAutonomy',{type:'doughnut',data:{labels:pm.map(x=>AUTO_LBL[x[0]]||x[0]),
    datasets:[{data:pm.map(x=>x[1]),backgroundColor:pm.map(x=>AUTO_CLR[x[0]]||C.slate),
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  const sub=d.subagents||[];
  mk('cDelegation',{type:'doughnut',data:{labels:sub.map(x=>x[0]),
    datasets:[{data:sub.map(x=>x[1]),backgroundColor:PIE,
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  mk('cTools',{type:'bar',data:{labels:d.tools.map(x=>x[0]),
    datasets:[{data:d.tools.map(x=>x[1]),backgroundColor:C.coral,borderRadius:5}]},
    options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
  mk('cSlash',{type:'bar',data:{labels:d.slash.map(x=>x[0]),
    datasets:[{data:d.slash.map(x=>x[1]),backgroundColor:C.teal,borderRadius:5}]},
    options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
  document.querySelector('#tModels tbody').innerHTML=d.models.length?d.models.map(m=>
    '<tr><td><b>'+esc(m.name)+'</b></td><td class="num">'+m.msgs+'</td><td class="num">'+
    fmt(m.input)+'</td><td class="num">'+fmt(m.output)+'</td><td class="num">'+m.cost+'</td></tr>').join('')
    :'<tr><td colspan="5" class="empty">No model activity in this range.</td></tr>';
}

/* ─────────────────────────── Projects ─────────────────────────── */
const ARROW='<svg class="rowarrow" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';
function renderProjects(){
  const d=D;
  document.querySelector('#tProj tbody').innerHTML=d.projects.length?d.projects.map(p=>
    '<tr tabindex="0"><td title="'+esc(p.name)+'"><b>'+esc(p.name.split('/').pop()||p.name)+
    '</b></td><td class="num">'+fmt(p.input)+'</td><td class="num">'+fmt(p.output)+
    '</td><td class="num">'+fmt(p.loc)+'</td><td class="num">'+p.cost+
    '</td><td>'+(p.has_instructions?'<span class="up">&#10003; CLAUDE.md</span>':'<span class="flat-d">&mdash;</span>')+
    '</td><td class="num">'+ARROW+'</td></tr>').join('')
    :'<tr><td colspan="7" class="empty">No projects in this range.</td></tr>';
  document.querySelectorAll('#tProj tbody tr').forEach((tr,i)=>{
    if(!d.projects[i])return;
    tr.onclick=()=>openProject(d.projects[i]);
    tr.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openProject(d.projects[i]);}};});
  document.querySelector('#tBranch tbody').innerHTML=d.branches.length?d.branches.map(b=>
    '<tr><td><b>'+esc(b.name)+'</b></td><td class="num">'+b.msgs+'</td><td class="num">'+
    fmt(b.output)+'</td><td class="num">'+fmt(b.loc)+'</td></tr>').join('')
    :'<tr><td colspan="4" class="empty">No branch data in this range.</td></tr>';
  document.querySelector('#tFiles tbody').innerHTML=d.top_files.length?d.top_files.map(f=>
    '<tr><td>'+esc(f[0])+'</td><td class="num">'+fmt(f[1])+'</td></tr>').join('')
    :'<tr><td colspan="2" class="empty">No files edited in this range.</td></tr>';
  $('lPrompts').innerHTML=d.longest_prompts.length?d.longest_prompts.map(p=>
    '<li>'+esc(p.preview)+'…<div class="mt"><b>'+p.lines+' lines</b> · '+p.words+
    ' words · '+p.day+'</div></li>').join('')
    :'<li class="empty">No prompts in this range.</li>';
}

/* ─────────────────────────── Weekly report ─────────────────────────── */
function buildPct(w){const t=w.reads+w.writes;return t?Math.round(w.writes/t*100):0;}

function fluencyOf(w){
  const sess=w.sessions,code=sess.filter(s=>s.loc>0);
  const lev=w.human_tokens?w.output/w.human_tokens:0;
  const codeShare=sess.length?code.length/sess.length:0;
  const delegation=Math.round(100*(.5*codeShare+.5*Math.min(1,lev/50)));
  const avgW=w.prompts?(w.prompt_words||0)/w.prompts:0;
  const oneShot=w.prompts?Math.max(0,1-((w.corrections||0)+w.interruptions)/w.prompts):0;
  const sweet=avgW<=0?0:avgW<8?avgW/8:avgW<=150?1:Math.max(0,1-(avgW-150)/300);
  const description=Math.round(100*(.65*oneShot+.35*sweet));
  const engaged=Math.min(1,((w.corrections||0)+w.interruptions)/Math.max(1,w.prompts*.05));
  const errCtl=Math.max(0,1-w.tool_errors/Math.max(1,w.writes||1));
  const discernment=Math.round(100*(.5*engaged+.5*errCtl));
  const verified=code.length?code.filter(s=>s.bash>0).length/code.length:0;
  const diligence=code.length?Math.round(100*verified):0;
  return{delegation,description,discernment,diligence,lev,codeShare,avgW,oneShot,engaged,verified,ncode:code.length};}

function fluencyTips(f){return{
  delegation:f.delegation<50?'Try handing Claude whole tasks (implement + verify), not just questions.':'Healthy mix of asking and delegating real work.',
  description:f.description<50?'Front-load constraints, file paths and expected behavior in the first prompt.':'Most prompts land without needing a correction.',
  discernment:f.engaged<.3?'You rarely push back — spot-check diffs and say so when output misses.':(f.discernment<50?'High error rate — slow down and review output before continuing.':'You actively steer and catch issues.'),
  diligence:f.ncode===0?'No code sessions this week.':(f.diligence<50?'Ask Claude to run tests/commands after edits — most code sessions never verified.':'Most code sessions verified their work with commands.')};}

function renderFluency(w,prev){
  const f=fluencyOf(w),tips=fluencyTips(f),fp=prev?fluencyOf(prev):null;
  const dims=[
    ['Delegation',f.delegation,'<b>'+Math.round(f.codeShare*100)+'%</b> of sessions shipped code · <b>'+f.lev.toFixed(1)+'×</b> leverage'],
    ['Description',f.description,'<b>'+Math.round(f.oneShot*100)+'%</b> of prompts needed no correction · <b>'+Math.round(f.avgW)+'</b> words avg'],
    ['Discernment',f.discernment,'<b>'+(w.corrections||0)+'</b> corrections + <b>'+w.interruptions+'</b> interruptions · <b>'+w.tool_errors+'</b> tool errors'],
    ['Diligence',f.diligence,'<b>'+Math.round(f.verified*100)+'%</b> of '+f.ncode+' code session'+(f.ncode===1?'':'s')+' ran commands after edits']];
  const key=['delegation','description','discernment','diligence'];
  $('flList').innerHTML=dims.map((d,i)=>
    '<div class="flrow"><span class="fln">'+d[0]+'</span>'+
    '<span class="flb"><span class="bar"><i style="width:'+d[1]+'%"></i></span></span>'+
    '<span class="fls">'+d[1]+'</span>'+
    '<span class="flt">'+d[2]+'<br>'+tips[key[i]]+'</span></div>').join('');
  const ds=[{label:'This week',data:dims.map(d=>d[1]),borderColor:C.coral,
    backgroundColor:hexa(C.coral,.17),pointBackgroundColor:C.coral,borderWidth:2}];
  if(fp)ds.push({label:'Last week',data:[fp.delegation,fp.description,fp.discernment,fp.diligence],
    borderColor:C.sky,backgroundColor:hexa(C.sky,.07),pointBackgroundColor:C.sky,borderWidth:1.5});
  mk('cFluency',{type:'radar',data:{labels:dims.map(d=>d[0]),datasets:ds},
    options:{scales:{r:{min:0,max:100,ticks:{display:false,stepSize:25},
      grid:{color:C.grid},angleLines:{color:C.grid},
      pointLabels:{color:C.txt2,font:{size:11.5,weight:'600'}}}},
      plugins:{legend:{position:'bottom'}}}});}

let reflQtext='',reflWeekKey=null;
function pickReflection(w,idx){
  const c=[];
  const corrRate=w.prompts?(w.corrections||0)/w.prompts:0;
  if(corrRate>0.08)c.push('You corrected Claude '+w.corrections+' times this week ('+Math.round(corrRate*100)+'% of prompts). What context could your first prompts include so the second try isn’t needed?');
  const late=w.messages?(w.late_msgs||0)/w.messages:0;
  if(late>0.2)c.push(Math.round(late*100)+'% of this week’s activity happened between 11pm and 6am. Is late-night coding a deliberate choice — or a habit worth questioning?');
  const tot=w.reads+w.writes,ex=tot?w.reads/tot:0;
  if(tot&&ex>0.65)c.push(Math.round(ex*100)+'% of tool calls this week were exploration. Are you using Claude mostly to understand code — and is there building you could delegate too?');
  if(tot&&ex<0.15)c.push(Math.round((1-ex)*100)+'% of tool calls this week were edits. Are you reading and verifying what gets written — or shipping on trust?');
  const code=w.sessions.filter(s=>s.loc>0),ver=code.length?code.filter(s=>s.bash>0).length/code.length:1;
  if(code.length>2&&ver<0.4)c.push('Only '+Math.round(ver*100)+'% of code-writing sessions ran a command afterwards. How do you know this week’s '+fmt(w.loc)+' new lines actually work?');
  c.push('What’s one thing you want to keep doing yourself, even if Claude could do it faster?');
  return c[idx%c.length];}

function renderWeek(){
  const wk=D.weeks;
  if(!wk.length){$('secSub').textContent='No data yet.';
    $('wKpis').innerHTML='<div class="empty">No weeks recorded yet.</div>';return;}
  if(wIdx===null)wIdx=wk.length-1;
  const w=wk[wIdx],prev=wIdx>0?wk[wIdx-1]:null;
  $('wPrev').disabled=wIdx===0;$('wNext').disabled=wIdx===wk.length-1;
  updateChrome();
  const bp=buildPct(w);
  const kpis=[
    ['Sessions',w.sessions.length,delta(w.sessions.length,prev&&prev.sessions.length),C.sky],
    ['Focus hours',(w.minutes/60).toFixed(1),delta(w.minutes,prev&&prev.minutes),C.teal],
    ['Output tokens',fmt(w.output),delta(w.output,prev&&prev.output),C.emerald],
    ['Lines of code',fmt(w.loc),delta(w.loc,prev&&prev.loc),C.violet],
    ['Prompts',fmt(w.prompts),delta(w.prompts,prev&&prev.prompts),C.coral],
    ['Building',bp+'%',(100-bp)+'% exploration',C.amber]];
  $('wKpis').innerHTML=kpis.map(k=>
    '<div class="kpi plain" style="--kc:'+k[3]+'"><div class="khead"><span class="eyebrow">'+k[0]+
    '</span></div><div class="num-lg">'+k[1]+'</div><div class="s">'+(k[2]||'')+'</div></div>').join('');
  const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
  const keys=Object.keys(g);
  $('wWork').innerHTML=keys.length?keys.map(p=>
    '<div class="grp"><h4>'+esc(p)+'</h4><ul>'+g[p].map(s=>
      '<li>'+esc(s.title)+'<div class="meta">'+fmtD(s.day)+' · '+s.min+' min · '+
      fmt(s.loc)+' LoC · '+s.prompts+' prompts</div></li>').join('')+'</ul></div>').join('')
    :'<div class="empty">No sessions logged this week.</div>';
  mk('cSplit',{type:'doughnut',data:{labels:['Building (file edits)','Exploration (reads/searches)'],
    datasets:[{data:[w.writes,w.reads],backgroundColor:[C.emerald,C.sky],
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  mk('cWeekDays',{type:'bar',data:{labels:w.days.map(x=>fmtD(x.day)),datasets:[
    {label:'Output tokens',data:w.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
    {label:'LoC',data:w.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
    options:{scales:{x:XGRID,y:{position:'left',...GRID},
      y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
      plugins:{legend:{position:'bottom'}}}});
  renderFluency(w,prev);
  reflQtext=pickReflection(w,wIdx);
  $('reflQ').textContent=reflQtext;
  /* clear the discussion only when switching weeks — not on the 15s refresh */
  if(reflWeekKey!==w.key){reflWeekKey=w.key;$('reflOut').textContent='';}
}
$('wPrev').onclick=()=>{if(wIdx>0){wIdx--;renderWeek();}};
$('wNext').onclick=()=>{if(D&&wIdx<D.weeks.length-1){wIdx++;renderWeek();}};

$('reflBtn').onclick=async()=>{
  if(!D||!D.weeks.length||!reflQtext)return;
  const btn=$('reflBtn'),out=$('reflOut'),old=btn.innerHTML;
  btn.disabled=true;btn.textContent='Thinking…';
  out.textContent='Running claude -p — this can take a minute…';
  try{const r=await fetch('/api/reflect?week='+encodeURIComponent(D.weeks[wIdx].key)+
    '&q='+encodeURIComponent(reflQtext));
    const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
  catch(e){out.textContent='Failed: '+e;}
  btn.disabled=false;btn.innerHTML=old;};

function weekMarkdown(w){
  const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
  let md='# Weekly Update — '+fmtD(w.start)+' to '+fmtD(w.end)+'\n\n## What I worked on\n';
  for(const p in g){md+='\n### '+p+'\n';g[p].forEach(s=>{md+='- '+s.title+' ('+s.day+', ~'+s.min+' min)\n';});}
  const bp=buildPct(w);
  md+='\n## Numbers\n';
  md+='- '+w.sessions.length+' sessions, ~'+(w.minutes/60).toFixed(1)+' focus-hours\n';
  md+='- '+w.loc.toLocaleString()+' lines of code written with Claude\n';
  md+='- '+w.prompts.toLocaleString()+' prompts · '+w.output.toLocaleString()+' tokens generated\n';
  md+='- Work split: '+bp+'% building / '+(100-bp)+'% exploration\n';
  const tc={};w.sessions.forEach(s=>{const t=s.type||'other';tc[t]=(tc[t]||0)+1});
  const mix=TT.filter(t=>tc[t]).map(t=>tc[t]+' '+t).join(', ');
  if(mix)md+='- Task mix: '+mix+'\n';
  return md;}

async function copyText(txt){
  try{await navigator.clipboard.writeText(txt);}
  catch(e){const ta=document.createElement('textarea');ta.value=txt;
    document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}}
function flash(btn,label){const old=btn.innerHTML;btn.textContent=label;
  setTimeout(()=>{btn.innerHTML=old;},1600);}

$('wCopy').onclick=async()=>{if(!D||!D.weeks.length)return;
  await copyText(weekMarkdown(D.weeks[wIdx]));flash($('wCopy'),'✓ Copied');};

$('wAI').onclick=async()=>{
  if(!D||!D.weeks.length)return;
  const btn=$('wAI'),card=$('wAIcard'),out=$('wAItext'),old=btn.innerHTML;
  btn.disabled=true;btn.textContent='Generating…';card.style.display='';
  out.textContent='Running claude -p — this can take a minute…';
  try{const r=await fetch('/api/ai_summary?week='+encodeURIComponent(D.weeks[wIdx].key));
    const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
  catch(e){out.textContent='Failed: '+e;}
  btn.disabled=false;btn.innerHTML=old;};

/* ═══════════════════ Project sheet: grabbable, velocity-aware ═══════════════════
   Open/close is a spring on `openP`; the drag offset is a second, independent
   spring. Dragging jumps the offset 1:1 with the finger (cancelling any
   in-flight animation), release projects the momentum forward and either
   dismisses or springs home carrying the release velocity. */
const sheet=$('sheet'),scrim=$('scrim'),sbox=$('sBox');
let sheetOn=false;
function paintSheet(){
  const p=openP.v,y=dragY.v;
  sbox.style.transform='translate3d(0,'+(y+(1-p)*22).toFixed(2)+'px,0) scale('+(.965+.035*p).toFixed(4)+')';
  sbox.style.opacity=Math.max(0,Math.min(1,p)).toFixed(3);
  sbox.style.filter=p<.999?'blur('+((1-p)*7).toFixed(2)+'px)':'none';
  scrim.style.opacity=(Math.max(0,Math.min(1,p))*Math.max(0,1-Math.max(0,y)/420)).toFixed(3);
}
const openP=new Spring(0,paintSheet,{damping:1,response:.36});
const dragY=new Spring(0,paintSheet,{damping:.8,response:.34});
openP.done=()=>{if(openP.target===0&&!sheetOn){sheet.style.display='none';scrim.style.display='none';}};

function openSheet(){sheetOn=true;sheet.style.display='flex';scrim.style.display='block';
  sheet.scrollTop=0;dragY.jump(0);paintSheet();openP.to(1);
  setTimeout(()=>$('sClose').focus(),60);}
function closeSheet(vel){
  if(!sheetOn)return;sheetOn=false;
  openP.to(0);dragY.to(Math.max(260,innerHeight*.45),vel);}
$('sClose').onclick=()=>closeSheet();
scrim.onclick=()=>closeSheet();
sheet.addEventListener('click',e=>{if(e.target===sheet)closeSheet();});
addEventListener('keydown',e=>{if(e.key==='Escape'){if(popOpen)togglePop(false);else if(sheetOn)closeSheet();}});

/* 1:1 tracking with grab offset, rubber-banding upward, momentum on release */
(function(){
  const grab=$('sGrab');let pid=null,startY=0,base=0,hist=[];
  grab.addEventListener('pointerdown',e=>{
    if(e.target.closest('#sClose'))return;
    pid=e.pointerId;grab.setPointerCapture(pid);
    startY=e.clientY;base=dragY.v;hist=[[e.clientY,performance.now()]];});
  grab.addEventListener('pointermove',e=>{
    if(pid===null||e.pointerId!==pid)return;
    let dy=base+(e.clientY-startY);
    if(dy<0)dy=-rubber(-dy,innerHeight);          // soft boundary, never a hard stop
    dragY.jump(dy);                                // continuous feedback, not on release
    hist.push([e.clientY,performance.now()]);if(hist.length>6)hist.shift();});
  const end=e=>{
    if(pid===null||(e&&e.pointerId!==pid))return;
    try{grab.releasePointerCapture(pid);}catch(err){}
    pid=null;
    let v=0;
    if(hist.length>1){const a=hist[0],b=hist[hist.length-1],dt=(b[1]-a[1])/1000;
      if(dt>0.001)v=(b[0]-a[0])/dt;}
    const projected=dragY.v+projectTo(v);          // where the flick is going
    if(projected>170||v>900)closeSheet(v);
    else dragY.to(0,v);};                          // hand the velocity to the spring
  grab.addEventListener('pointerup',end);
  grab.addEventListener('pointercancel',end);
})();

function openProject(p){
  $('sTitle').textContent=p.name.split('/').pop()||p.name;
  $('sSub').innerHTML=esc(p.name)+' · '+fmt(p.input)+' in · '+fmt(p.output)+' out · '+
    fmt(p.loc)+' LoC · '+p.sessions+' sessions · est. $'+p.cost;
  openSheet();
  mk('cProj',{type:'bar',data:{labels:p.days.map(x=>x.day.slice(5)),datasets:[
    {label:'Output tokens',data:p.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
    {label:'LoC',data:p.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
    options:{scales:{x:XGRID,y:{position:'left',...GRID},
      y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
      plugins:{legend:{position:'bottom'}}}});
  const sess=(D.sessions_list||[]).filter(s=>s.project===p.name);
  $('sSess').innerHTML=sess.length?sess.map(s=>
    '<li><b>'+esc(s.title)+'</b><div class="mt">'+s.day+' · '+s.min+' min · '+fmt(s.loc)+
    ' LoC · '+s.prompts+' prompts · '+fmt(s.output)+' tokens out</div></li>').join('')
    :'<li class="empty">No session details available.</li>';
  document.querySelector('#sFiles tbody').innerHTML=(p.files||[]).length?(p.files||[]).map(f=>
    '<tr><td>'+esc(f[0])+'</td><td class="num">'+fmt(f[1])+'</td></tr>').join('')
    :'<tr><td colspan="2" class="empty">No files recorded.</td></tr>';
}

/* ─────────────────────────── Exports ─────────────────────────── */
function download(name,text,type){
  const b=new Blob([text],{type}),u=URL.createObjectURL(b);
  const a=document.createElement('a');a.href=u;a.download=name;
  document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u);}
function rangeLabel(){return (customFrom||customTo)
  ?((customFrom||'start')+'_to_'+(customTo||'now'))
  :(days?('last'+days+'d'):'alltime');}
$('expJson').onclick=()=>{if(D)download('claude-usage_'+rangeLabel()+'.json',
  JSON.stringify(D,null,2),'application/json');};
$('expCsv').onclick=()=>{if(!D)return;
  const rows=[['date','input','output','cache_read','cache_write','loc','prompts','reads','writes','est_cost_usd']];
  D.days.forEach(x=>rows.push([x.day,x.input,x.output,x.cache_read,x.cache_write,
    x.loc,x.prompts,x.reads,x.writes,x.cost]));
  download('claude-usage_'+rangeLabel()+'.csv',rows.map(r=>r.join(',')).join('\n'),'text/csv');};
$('expCopy').onclick=async()=>{if(!D)return;const t=D.totals;
  await copyText('Claude Code usage — '+rangeLabel().replace(/_/g,' ')+'\n'+
    'Output tokens: '+t.output.toLocaleString()+'\nInput tokens: '+t.input.toLocaleString()+'\n'+
    'Cache read: '+t.cache_read.toLocaleString()+' ('+t.cache_efficiency+'% hit)\n'+
    'Lines of code: '+t.loc.toLocaleString()+'\nPrompts: '+t.prompts.toLocaleString()+'\n'+
    'Sessions: '+t.sessions+' ('+t.avg_session_min+' min avg)\n'+
    'Est. API cost: $'+t.cost+'\nLeverage: '+t.leverage+'x');
  flash($('expCopy'),'✓');};

/* ─────────────────────────── Boot ─────────────────────────── */
/* Chart.js has a rare first-paint race in its animator (creating several
   charts in one tick can throw asynchronously inside its own rAF loop,
   "this._fn is not a function"). It's not catchable at the call site since
   it happens after mk() returns — but a plain re-render always recovers,
   so treat it as a one-shot self-heal rather than a fatal error. */
let chartRecoverAt=0;
addEventListener('error',e=>{
  if(!/chart\.umd\.min\.js/.test(e.filename||''))return;
  const now=Date.now();
  if(now-chartRecoverAt<1000)return;   // avoid a retry storm if it keeps failing
  chartRecoverAt=now;
  if(D)render();
});
applyTheme(false);
updateChrome();
load();
setInterval(load,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    scanner: Scanner = None
    root: str = ""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode()
            self._send(200, "text/html; charset=utf-8", body)
        elif parsed.path == "/chart.umd.min.js":
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "chart.umd.min.js")
            try:
                with open(local, "rb") as f:
                    self._send(200, "application/javascript", f.read())
            except OSError:
                # fall back to the CDN if the local copy is missing
                self.send_response(302)
                self.send_header("Location",
                                 "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js")
                self.end_headers()
        elif parsed.path == "/api/stats":
            q = parse_qs(parsed.query)
            frm = (q.get("from") or [""])[0].strip()
            to = (q.get("to") or [""])[0].strip()
            try:
                days = int(q.get("days", ["30"])[0])
            except ValueError:
                days = 30
            # Build an all-time Stats (powers the Weekly tab, streaks and deltas)
            # plus a range-scoped Stats so every card on the dashboard respects
            # the selected window — not just the headline KPIs.
            full = self.scanner.build_stats()
            alldays = sorted(full.days.keys())
            prev = []
            if frm or to:                       # custom date range
                cur = [d for d in alldays if (not frm or d >= frm) and (not to or d <= to)]
                ranged = self.scanner.build_stats(frm or None, to or None)
            elif days > 0:                       # rolling N-day window
                cur = alldays[-days:]
                prev = alldays[-2 * days:-days]  # the window just before it (for deltas)
                ranged = self.scanner.build_stats(cur[0]) if cur else Stats()
            else:                                # all time
                cur = alldays
                ranged = full
            data = stats_to_json(ranged, None)
            data["weeks"] = build_weeks(full)
            data["root"] = self.root

            def sd(dl, k):
                return sum(full.days[d][k] for d in dl)

            if prev:
                data["prev"] = {k: sd(prev, k) for k in
                                ("output", "input", "loc", "prompts", "human_tokens", "cache_read",
                                 "corrections")}
                data["prev"]["cost"] = round(sd(prev, "cost"), 2)
                pset, pc = set(prev), 0
                for _sid, se in full.sessions.items():
                    try:
                        sday = datetime.fromisoformat(
                            se["start"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
                    except Exception:
                        continue
                    if sday in pset:
                        pc += 1
                data["prev"]["sessions"] = pc

            data["streak"], data["best_streak"] = active_streak(alldays)
            data["active_days"] = len(alldays)
            data["goals"] = GOALS
            watcher = getattr(self, "watcher", None)
            data["notifications"] = list(watcher.recent_events[-20:]) if watcher else []
            data["plan_window"] = watcher.plan_window_snapshot() if watcher else None
            if alldays:
                last = alldays[-1]
                data["today"] = {"day": last, "loc": full.days[last]["loc"],
                                 "tokens": full.days[last]["output"]}
                data["week_tokens"] = sum(full.days[d]["output"] for d in alldays[-7:])
            self._send(200, "application/json", json.dumps(data).encode())
        elif parsed.path == "/api/ai_summary":
            q = parse_qs(parsed.query)
            wkey = (q.get("week") or [""])[0]
            st = self.scanner.build_stats()
            week = next((w for w in build_weeks(st) if w["key"] == wkey), None)
            if not week:
                payload = {"ok": False, "error": "unknown week %r" % wkey}
            else:
                payload = ai_week_summary(week)
            self._send(200, "application/json", json.dumps(payload).encode())
        elif parsed.path == "/api/reflect":
            q = parse_qs(parsed.query)
            wkey = (q.get("week") or [""])[0]
            question = (q.get("q") or [""])[0].strip()[:500]
            st = self.scanner.build_stats()
            week = next((w for w in build_weeks(st) if w["key"] == wkey), None)
            if not week:
                payload = {"ok": False, "error": "unknown week %r" % wkey}
            elif not question:
                payload = {"ok": False, "error": "no question provided"}
            else:
                payload = ai_reflection(week, question)
            self._send(200, "application/json", json.dumps(payload).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence request logging
        pass


# ---------------------------------------------------------------------------
# Setup wizard (--setup-notifications): wires the statusLine hook so plan-
# usage % and toast milestones use real Anthropic numbers instead of the
# local estimate. See docs/superpowers/specs/2026-09-09-setup-notifications-
# onboarding-design.md for the full design.
# ---------------------------------------------------------------------------

STATUSLINE_MARKER = "dev_token_dashboard_statusline.js"


def claude_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def load_settings_json(path):
    """Like load_notify_state, but distinguishes a missing file (fine, an
    empty settings.json is valid) from a malformed one (must abort rather
    than silently treat a broken config as empty and overwrite it)."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON: {e}"
    except OSError as e:
        return None, f"could not read {path}: {e}"


def classify_statusline(settings):
    sl = settings.get("statusLine")
    if not isinstance(sl, dict) or not sl.get("command"):
        return "missing"
    if STATUSLINE_MARKER in sl["command"]:
        return "ours"
    return "foreign"


def merge_statusline_config(settings, statusline_js_path):
    new_settings = dict(settings)
    if classify_statusline(settings) == "missing":
        new_settings["statusLine"] = {
            "type": "command",
            "command": f'node "{statusline_js_path}"',
            "refreshInterval": 30,
        }
    else:  # "ours" -- caller must not call this for "foreign"
        new_settings["statusLine"] = dict(settings["statusLine"], refreshInterval=30)
    return new_settings


def node_available():
    return shutil.which("node") is not None


def write_settings_with_backup(settings_path, new_settings):
    backup_path = None
    if os.path.exists(settings_path):
        backup_path = f"{settings_path}.bak-{int(time.time())}"
        shutil.copy2(settings_path, backup_path)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new_settings, f, indent=2)
    os.replace(tmp, settings_path)
    return backup_path


# Written verbatim to <claude_config_dir>/dev_token_dashboard_statusline.js by
# run_setup_notifications() -- never to statusline.js or any user-chosen path.
# Trimmed from the hand-written ~/.claude/statusline.js already in use on the
# maintainer's machine: same merge-onto-previous-snapshot side effect, plus a
# minimal one-line status output for users who had no statusLine at all.
BUNDLED_STATUSLINE_JS = r"""// dev-token-dashboard:managed-statusline
// Captures Anthropic's real rate_limits (five_hour/seven_day) for
// dev_token_dashboard.py's plan-usage panel and toast notifications.
// See docs/DASHBOARD_GUIDE.md#plan-usage-limits.
const fs = require('fs');
const os = require('os');
const path = require('path');

let raw = '';
process.stdin.on('data', d => raw += d);
process.stdin.on('end', () => {
  let j = {};
  try { j = JSON.parse(raw); } catch (e) { /* fall through with j={} */ }

  if (j.rate_limits) {
    const outPath = path.join(os.homedir(), '.claude', 'rate_limits_latest.json');
    let merged = {};
    try {
      const prev = JSON.parse(fs.readFileSync(outPath, 'utf8'));
      merged = Object.assign({}, prev.rate_limits);
    } catch (e) { /* no previous snapshot, or unreadable -- start fresh */ }
    Object.assign(merged, j.rate_limits);
    const payload = JSON.stringify({
      rate_limits: merged,
      captured_at: Date.now() / 1000,
    });
    try {
      const tmp = outPath + '.tmp';
      fs.writeFileSync(tmp, payload);
      fs.renameSync(tmp, outPath);
    } catch (e) { /* best-effort; never break the status line over this */ }
  }

  const dir = (j.workspace && j.workspace.current_dir) || j.cwd || '';
  const model = (j.model && j.model.display_name) || '';
  const ctx = j.context_window && j.context_window.used_percentage;
  const rl = j.rate_limits || {};
  const five = rl.five_hour && rl.five_hour.used_percentage;
  const seven = rl.seven_day && rl.seven_day.used_percentage;

  let line = dir + '  ' + model;
  if (ctx != null) line += `  ctx ${Math.round(ctx)}%`;
  if (five != null) line += `  5h ${Math.round(five)}%`;
  if (seven != null) line += `  wk ${Math.round(seven)}%`;
  process.stdout.write(line);
});
"""


def _print_final_report(print_fn):
    fresh = load_rate_limits(RATE_LIMITS_PATH) is not None
    print_fn("")
    print_fn("Rate-limit capture:  " + ("ok, fresh" if fresh else
              "not yet captured (will appear after your next Claude Code message)"))
    print_fn("Toast notifications: " + ("ON" if NOTIFY.get("enabled", True) else "OFF") +
              '  (edit NOTIFY["enabled"] in the script to change)')


def run_setup_notifications(claude_dir=None, dry_run=False, input_fn=input,
                             print_fn=print, isatty_fn=None):
    isatty_fn = isatty_fn or sys.stdin.isatty
    claude_dir = claude_dir or claude_config_dir()
    settings_path = os.path.join(claude_dir, "settings.json")
    statusline_js_path = os.path.join(claude_dir, STATUSLINE_MARKER)

    if os.name != "nt":
        print_fn("Toast notifications aren't available on this OS yet -- "
                  "setting up rate-limit capture only.")

    settings, err = load_settings_json(settings_path)
    if err:
        print_fn(f"[!] {err}")
        print_fn("    Fix or remove this file, then re-run --setup-notifications.")
        return 1

    kind = classify_statusline(settings)

    if kind == "foreign":
        _report_foreign_statusline(settings, print_fn)
        _print_final_report(print_fn)
        return 0

    if not node_available():
        print_fn("[!] `node` was not found on PATH. Claude Code itself requires "
                  "Node.js, so this is unexpected -- install it and re-run.")
        return 1

    new_settings = merge_statusline_config(settings, statusline_js_path)
    if new_settings == settings:
        print_fn(f"Already configured -- {settings_path} needs no changes.")
        _print_final_report(print_fn)
        return 0

    print_fn("This will write:")
    print_fn(f"  {statusline_js_path}")
    print_fn(f"  statusLine block in {settings_path}:")
    print_fn(json.dumps(new_settings["statusLine"], indent=2))

    if dry_run:
        print_fn("Dry run -- nothing written.")
        return 0

    if not isatty_fn():
        print_fn("[!] Not running interactively -- re-run from a terminal, "
                  "or pass --dry-run to preview only.")
        return 1

    answer = input_fn("Proceed? [y/N] ").strip().lower()
    if answer != "y":
        print_fn("Cancelled -- nothing written.")
        return 0

    with open(statusline_js_path, "w", encoding="utf-8") as f:
        f.write(BUNDLED_STATUSLINE_JS)
    try:
        backup_path = write_settings_with_backup(settings_path, new_settings)
    except OSError as e:
        print_fn(f"[!] Could not write {settings_path}: {e}")
        print_fn(f"    {statusline_js_path} was written, but the statusLine hook "
                  "isn't wired in yet -- fix the permissions and re-run.")
        return 1
    if backup_path:
        print_fn(f"Backed up previous settings to {backup_path}")
    print_fn(f"Wrote {statusline_js_path} and updated {settings_path}.")
    _print_final_report(print_fn)
    return 0


def _extract_script_path(command):
    m = re.search(r'"([^"]+)"', command)
    return m.group(1) if m else None


def _report_foreign_statusline(settings, print_fn):
    cmd = settings["statusLine"]["command"]
    print_fn(f"An existing statusLine is already configured: {cmd}")
    script_path = _extract_script_path(cmd)
    try:
        if script_path and os.path.isfile(script_path):
            with open(script_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "rate_limits" in content:
                print_fn("Your existing script already looks like it references rate_limits.")
    except Exception:
        pass
    if load_rate_limits(RATE_LIMITS_PATH) is not None:
        print_fn(f"{RATE_LIMITS_PATH} already has a fresh capture -- you may already be covered.")
    else:
        print_fn(f"{RATE_LIMITS_PATH} has no fresh capture yet.")
    print_fn("To add rate-limit capture to your own script, make it write this JSON")
    print_fn("shape to ~/.claude/rate_limits_latest.json whenever `rate_limits` is")
    print_fn("present in its stdin input (see the reference script below):")
    print_fn(BUNDLED_STATUSLINE_JS)
    print_fn("Full explanation: docs/DASHBOARD_GUIDE.md#plan-usage-limits")


def default_root():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.join(env, "projects")
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def main():
    ap = argparse.ArgumentParser(description="Local Claude Code usage dashboard (zero token cost)")
    ap.add_argument("--dir", default=default_root(), help="Claude projects log dir")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dump", action="store_true", help="print stats JSON and exit (for testing)")
    ap.add_argument("--install-startup", action="store_true",
                    help="Windows: run dashboard automatically at logon (Task Scheduler)")
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="Windows: remove the logon task")
    ap.add_argument("--setup-notifications", action="store_true",
                    help="Wire up the statusLine hook for accurate plan-usage %% "
                         "and toast notifications (Windows)")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --setup-notifications, show what would change "
                         "without writing anything")
    args = ap.parse_args()

    if args.setup_notifications:
        sys.exit(run_setup_notifications(dry_run=args.dry_run))

    if args.install_startup or args.uninstall_startup:
        if os.name != "nt":
            sys.exit("Startup install is Windows-only.")
        import subprocess
        task = "DevTokenDashboard"
        if args.uninstall_startup:
            subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"])
            print("Removed logon task.")
            return
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        script = os.path.abspath(__file__)
        cmd = f'"{pyw}" "{script}" --no-browser --port {args.port}'
        r = subprocess.run(["schtasks", "/Create", "/TN", task, "/TR", cmd,
                            "/SC", "ONLOGON", "/F"])
        if r.returncode == 0:
            print(f"Installed: dashboard will start at logon on port {args.port}.")
            print("Remove anytime with:  python dev_token_dashboard.py --uninstall-startup")
        else:
            print("Failed — try running the terminal as Administrator.")
        return

    if not os.path.isdir(args.dir):
        print(f"[!] Log directory not found: {args.dir}")
        print("    Pass it explicitly:  python dev_token_dashboard.py --dir <path>")
        sys.exit(1)

    scanner = Scanner(args.dir)

    if args.dump:
        print(json.dumps(stats_to_json(scanner.build_stats(), None), indent=2))
        return

    watcher = NotificationWatcher(scanner)

    Handler.scanner = scanner
    Handler.root = args.dir
    Handler.watcher = watcher
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"  Dev Token Dashboard running at {url}")
    print(f"  Reading logs from: {args.dir}")
    print("  Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def _notify_loop():
        while True:
            try:
                watcher.poll_once()
            except Exception as e:
                print(f"[warn] notification watcher error: {e}")
            time.sleep(NOTIFY["poll_seconds"])

    threading.Thread(target=_notify_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye!")


if __name__ == "__main__":
    main()
