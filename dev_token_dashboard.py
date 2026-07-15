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
        projects.append({
            "name": name, "input": p["input"], "output": p["output"],
            "loc": p["loc"], "prompts": p["prompts"],
            "sessions": len(p["sessions"]), "cost": round(p["cost"], 2),
            "days": [{"day": d, **p["days"][d]} for d in pdays],
            "files": p["files"].most_common(8),
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="/chart.umd.min.js"></script>
<style>
:root{
--bg:#0a0c11;--bg-2:#0e1117;--surface:#141821;--surface-2:#1a1f2b;--elevated:#1e2431;
--border:rgba(255,255,255,.07);--border-strong:rgba(255,255,255,.13);
--txt:#e7edf5;--txt-2:#aab4c4;--dim:#7c8798;
--acc:#e8916b;--acc-2:#d97757;--acc-soft:rgba(232,145,107,.14);
--sky:#56a3f5;--violet:#a78bfa;--emerald:#34d399;--amber:#fbbf24;--rose:#fb7185;--teal:#2dd4bf;
--green:#34d399;--orange:#fbbf24;--purple:#a78bfa;--red:#fb7185;
--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px -16px rgba(0,0,0,.7);--radius:16px}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--txt);
font:14px/1.55 'Segoe UI',-apple-system,BlinkMacSystemFont,Inter,Roboto,sans-serif;
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;min-height:100vh;padding:0 0 46px;
background-image:radial-gradient(900px 520px at 10% -10%,rgba(232,145,107,.10),transparent 60%),radial-gradient(820px 520px at 100% -4%,rgba(86,163,245,.08),transparent 55%);
background-attachment:fixed}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-thumb{background:#272e3b;border-radius:8px;border:3px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#333c4c}
.wrap{max-width:1360px;margin:0 auto;padding:0 24px}
code{font:12px 'Cascadia Code',Consolas,monospace;color:var(--txt-2)}
header.top{position:sticky;top:0;z-index:50;background:rgba(10,12,17,.72);
backdrop-filter:blur(14px) saturate(150%);-webkit-backdrop-filter:blur(14px) saturate(150%);
border-bottom:1px solid var(--border)}
.top .wrap{display:flex;align-items:center;gap:14px;height:64px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;
background:linear-gradient(145deg,var(--acc),var(--acc-2));box-shadow:0 6px 18px -6px rgba(232,145,107,.6);color:#1a0f0a}
.logo svg{width:21px;height:21px}
.brand .t{font-size:16px;font-weight:600;letter-spacing:-.2px}
.brand .st{font-size:11.5px;color:var(--dim);margin-top:1px}
.grow{flex:1}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--txt-2);
background:var(--surface);border:1px solid var(--border);padding:6px 12px;border-radius:999px;white-space:nowrap}
.pill svg{width:13px;height:13px;color:var(--dim)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--emerald);animation:pulse 2.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.5)}70%{box-shadow:0 0 0 7px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.tabs{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:4px;gap:4px;margin:24px 0 18px}
.tabs button{background:transparent;border:none;color:var(--dim);padding:8px 18px;border-radius:9px;
cursor:pointer;font-size:13.5px;font-weight:500;display:inline-flex;align-items:center;gap:7px;transition:.18s}
.tabs button svg{width:15px;height:15px}
.tabs button:hover{color:var(--txt-2)}
.tabs button.on{color:var(--txt);background:var(--elevated);box-shadow:var(--shadow)}
#alerts:not(:empty){margin-bottom:16px}
.parsewarn{color:var(--amber);font-size:12px;background:rgba(251,191,36,.08);
border:1px solid rgba(251,191,36,.25);padding:9px 13px;border-radius:10px;display:inline-flex;gap:8px;align-items:center}
.rangebar{display:flex;align-items:center;gap:12px;margin-bottom:22px;flex-wrap:wrap}
.rangebar .lab{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px;font-weight:600}
.rangebar .grow{flex:1}
.rnote{font-size:11.5px;color:var(--dim);margin:-10px 0 20px}
.custom{display:inline-flex;align-items:center;gap:6px}
.custom input[type=date]{background:var(--surface);border:1px solid var(--border);color:var(--txt-2);
border-radius:8px;padding:6px 9px;font:inherit;font-size:12px;color-scheme:dark}
.custom input[type=date]:focus{outline:none;border-color:var(--acc)}
.custom .dash{color:var(--dim)}
.mom{display:flex;gap:8px 26px;flex-wrap:wrap;align-items:center}
.mom .mblock{display:flex;align-items:center;gap:12px}
.mom .mico{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;
background:var(--acc-soft);color:var(--acc)}
.mom .mico svg{width:22px;height:22px}
.mom .mv{font-size:24px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums;line-height:1}
.mom .ml{font-size:11.5px;color:var(--dim);margin-top:3px}
.mom .sep{width:1px;align-self:stretch;background:var(--border)}
.mom .goals{display:flex;gap:20px;flex-wrap:wrap;flex:1;min-width:220px;justify-content:flex-end}
.goalw{min-width:150px}
.goalw .gt{display:flex;justify-content:space-between;font-size:11.5px;color:var(--dim);margin-bottom:6px}
.goalw .gt b{color:var(--txt);font-weight:600;font-variant-numeric:tabular-nums}
.bar{height:8px;background:rgba(255,255,255,.06);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--acc),var(--acc-2));transition:width .5s ease}
.bar.over>i{background:linear-gradient(90deg,var(--amber),var(--rose))}
.kpi .kd{font-size:11px;margin-top:6px;font-variant-numeric:tabular-nums}
.range{display:inline-flex;gap:4px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:4px}
.range button{background:transparent;border:none;color:var(--dim);padding:6px 14px;border-radius:7px;
cursor:pointer;font-size:12.5px;font-weight:500;transition:.16s}
.range button:hover{color:var(--txt-2)}
.range button.on{color:#1a0f0a;background:linear-gradient(145deg,var(--acc),var(--acc-2));font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(198px,1fr));gap:14px;margin-bottom:24px}
.kpi{position:relative;background:linear-gradient(160deg,var(--surface),var(--bg-2));
border:1px solid var(--border);border-radius:var(--radius);padding:16px 16px 15px;overflow:hidden;
transition:transform .2s,border-color .2s;box-shadow:var(--shadow)}
.kpi:hover{transform:translateY(-2px);border-color:var(--border-strong)}
.kpi::before{content:"";position:absolute;inset:0 0 auto 0;height:2px;
background:linear-gradient(90deg,var(--kc,var(--acc)),transparent 72%)}
.kpi .khead{display:flex;align-items:center;justify-content:space-between;margin-bottom:13px}
.kpi .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px;font-weight:600}
.kpi .ic{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:var(--kbg,var(--acc-soft));color:var(--kc,var(--acc))}
.kpi .ic svg{width:16px;height:16px}
.kpi .v{font-size:26px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums;line-height:1}
.kpi .s{color:var(--dim);font-size:11.5px;margin-top:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:16px}
.card{background:linear-gradient(170deg,var(--surface),var(--bg-2));border:1px solid var(--border);
border-radius:var(--radius);padding:18px 18px 16px;box-shadow:var(--shadow);transition:border-color .2s}
.card:hover{border-color:var(--border-strong)}
.card.wide{grid-column:1/-1}
.card h3{font-size:13px;font-weight:600;margin-bottom:16px;color:var(--txt);display:flex;align-items:center;gap:9px}
.card h3 svg{width:16px;height:16px;color:var(--acc);flex:none}
.card h3 .hint{color:var(--dim);font-weight:400;font-size:11.5px;margin-left:auto}
canvas{max-height:288px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--border)}
thead th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
tbody tr{transition:background .13s}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,.025)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td b{color:var(--txt);font-weight:600}
#tProj tbody tr{cursor:pointer}
#tProj tbody tr:hover{background:var(--acc-soft)}
.hm{display:grid;grid-template-columns:38px repeat(24,1fr);gap:3px;font-size:10px;color:var(--dim);align-items:center}
.hm .cell{aspect-ratio:1;border-radius:4px;background:rgba(255,255,255,.03);transition:outline .12s}
.hm .cell:hover{outline:1.5px solid var(--acc);outline-offset:1px}
.hm .hh{text-align:center;font-size:9.5px}
.hm .wd{font-size:10.5px;font-weight:500}
.hleg{display:flex;align-items:center;gap:7px;margin-top:12px;font-size:11px;color:var(--dim);justify-content:flex-end}
.hleg i{width:13px;height:13px;border-radius:3px;display:inline-block}
.plist{list-style:none}
.plist li{margin-bottom:10px;color:var(--txt-2);font-size:12.5px;padding:10px 12px;
background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:10px;border-left:2px solid var(--acc)}
.plist li:last-child{margin-bottom:0}
.plist b{color:var(--txt);font-weight:600}
.plist .mt{color:var(--dim);font-size:11px;margin-top:3px}
footer{color:var(--dim);font-size:11.5px;margin-top:30px;padding-top:18px;border-top:1px solid var(--border);line-height:1.7}
.wknav{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap}
.wknav h2{font-size:16px;font-weight:600;min-width:220px}
.wknav .sp{flex:1}
.btn{background:var(--surface);border:1px solid var(--border);color:var(--txt-2);padding:8px 14px;border-radius:10px;
cursor:pointer;font-size:12.5px;font-weight:500;display:inline-flex;align-items:center;gap:7px;transition:.16s}
.btn svg{width:14px;height:14px}
.btn:hover{border-color:var(--border-strong);color:var(--txt)}
.btn.act{background:var(--acc-soft);border-color:rgba(232,145,107,.4);color:var(--acc)}
.btn:disabled{opacity:.35;cursor:default}
.worklist h4{color:var(--acc);font-size:12px;font-weight:600;margin:16px 0 8px;text-transform:uppercase;letter-spacing:.5px}
.worklist h4:first-child{margin-top:0}
.worklist ul{list-style:none}
.worklist li{padding:9px 0;border-bottom:1px solid var(--border);font-size:13px}
.worklist li:last-child{border-bottom:none}
.worklist li .meta{color:var(--dim);font-size:11.5px;margin-top:2px}
.aiout{white-space:pre-wrap;font-size:13.5px;line-height:1.7;color:var(--txt-2)}
.note{color:var(--dim);font-size:11px;margin-top:12px;line-height:1.6}
#modal{position:fixed;inset:0;background:rgba(5,7,10,.7);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
display:none;z-index:100;align-items:flex-start;justify-content:center;padding:48px 16px;overflow:auto}
#modal .mbox{background:linear-gradient(170deg,var(--surface-2),var(--bg-2));border:1px solid var(--border-strong);
border-radius:18px;padding:26px;max-width:860px;width:100%;position:relative;box-shadow:0 24px 60px -20px rgba(0,0,0,.8)}
#modal h2{font-size:16px;font-weight:600;margin-bottom:5px;color:var(--txt)}
#modal .msub{color:var(--dim);font-size:12px;margin-bottom:18px}
#modal h3{font-size:11px;font-weight:600;margin:20px 0 10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
#mClose{position:absolute;top:16px;right:18px;width:32px;height:32px;border-radius:9px;background:var(--surface);
border:1px solid var(--border);color:var(--dim);font-size:18px;cursor:pointer;transition:.16s;display:grid;place-items:center}
#mClose:hover{color:var(--txt);border-color:var(--border-strong)}
.facts{display:flex;gap:10px 34px;flex-wrap:wrap;margin-bottom:16px}
.facts .f .fv{font-size:21px;font-weight:700;letter-spacing:-.4px;font-variant-numeric:tabular-nums}
.facts .f .fl{font-size:11px;color:var(--dim);margin-top:2px}
.flgrid{display:grid;grid-template-columns:300px 1fr;gap:24px;align-items:center}
@media (max-width:900px){.flgrid{grid-template-columns:1fr}}
.flrow{display:flex;align-items:center;gap:14px;padding:11px 0;border-bottom:1px solid var(--border);flex-wrap:wrap}
.flrow:last-child{border-bottom:none}
.flrow .fln{width:104px;font-weight:600;font-size:13px;flex:none}
.flrow .flb{width:120px;flex:none}
.flrow .fls{width:34px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;flex:none}
.flrow .flt{flex:1;color:var(--dim);font-size:11.5px;line-height:1.5;min-width:200px}
.flrow .flt b{color:var(--txt-2);font-weight:600}
.reflq{font-size:15px;line-height:1.65;color:var(--txt);background:var(--acc-soft);
border-left:3px solid var(--acc);padding:14px 16px;border-radius:10px;margin-bottom:14px}
@media (max-width:640px){.wrap{padding:0 14px}.cards{grid-template-columns:1fr}.card{padding:15px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style></head><body>
<header class="top"><div class="wrap">
<div class="brand">
<div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg></div>
<div><div class="t">Dev Token Dashboard</div><div class="st">Local Claude Code usage analytics</div></div>
</div>
<div class="grow"></div>
<span class="pill" title="Reads the logs Claude Code already writes locally — makes no API calls"><span class="dot"></span>Live &middot; 0 tokens</span>
<span class="pill"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Updated <span id="upd">…</span></span>
</div></header>

<div class="wrap">
<div class="tabs"><button id="tabDash" class="on"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>Dashboard</button><button id="tabWeek"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>Weekly Report</button></div>
<div id="alerts"></div>

<div id="viewDash">
<div class="rangebar"><span class="lab">Range</span>
<div class="range" id="range">
<button data-d="7">7 days</button><button data-d="30" class="on">30 days</button>
<button data-d="90">90 days</button><button data-d="0">All time</button></div>
<div class="custom"><input type="date" id="cFrom" title="From date"><span class="dash">&ndash;</span><input type="date" id="cTo" title="To date"><button class="btn" id="cApply">Apply</button></div>
<span class="grow"></span>
<button class="btn" id="expCopy" title="Copy a text summary to the clipboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy</button>
<button class="btn" id="expCsv" title="Download the daily table as CSV"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M12 18v-6M9 15l3 3 3-3"/></svg>CSV</button>
<button class="btn" id="expJson" title="Download the full stats payload as JSON"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>JSON</button>
</div>
<div class="rnote" id="rnote">Applies to every card below. The <b style="color:var(--txt-2)">Weekly Report</b> tab is always full history.</div>
<div class="card wide" id="momCard" style="margin-bottom:24px;display:none"><div class="mom" id="mom"></div></div>
<div class="grid" id="kpis"></div>
<div class="cards">
<div class="card wide"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8"/><rect x="12" y="6" width="3" height="12"/><rect x="17" y="13" width="3" height="5"/></svg>Daily tokens<span class="hint">input &middot; output &middot; cache read</span></h3><canvas id="cTokens"></canvas></div>
<div class="card wide"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-9 4 18 3-9h4"/></svg>You vs Claude<span class="hint">tokens you typed vs Claude produced &middot; log scale</span></h3><canvas id="cBalance"></canvas></div>
<div class="card wide"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Time &amp; wellness<span class="hint">active time from 10-min activity buckets &middot; quiet hours 23:00&ndash;06:00</span></h3><div class="facts" id="wellFacts"></div><canvas id="cActive" style="max-height:200px"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg>Model usage<span class="hint">output tokens</span></h3><canvas id="cModels"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg>Lines of code / day<span class="hint">written by Claude</span></h3><canvas id="cLoc"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 3 8 21M16 3l-3 18M4 9h16M3 15h16"/></svg>Exploration vs building<span class="hint">tool calls / day</span></h3><canvas id="cRW"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>Friction / day<span class="hint">interruptions &amp; tool errors</span></h3><canvas id="cFrict"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>Task mix / day<span class="hint">sessions classified by title</span></h3><canvas id="cTaskMix"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h7v7M4 20 21 4M4 4l16 16"/></svg>Tool usage</h3><canvas id="cTools"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6 3 12l6 6M15 6l6 6-6 6"/></svg>Slash commands</h3><canvas id="cSlash"></canvas></div>
<div class="card wide"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>Activity heatmap<span class="hint">messages by weekday &times; hour</span></h3><div class="hm" id="heat"></div><div class="hleg">Less<i style="background:rgba(232,145,107,.15)"></i><i style="background:rgba(232,145,107,.4)"></i><i style="background:rgba(232,145,107,.65)"></i><i style="background:rgba(232,145,107,.9)"></i>More</div></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7 12 3l9 4-9 4-9-4zM3 12l9 4 9-4M3 17l9 4 9-4"/></svg>Projects<span class="hint">click a row for details</span></h3><table id="tProj"><thead><tr>
<th>Project</th><th class="num">In</th><th class="num">Out</th><th class="num">LoC</th><th class="num">Est. $</th></tr></thead><tbody></tbody></table></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg>Model breakdown</h3><table id="tModels"><thead><tr>
<th>Model</th><th class="num">Msgs</th><th class="num">In</th><th class="num">Out</th><th class="num">Est. $</th></tr></thead><tbody></tbody></table></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="8" r="3"/><path d="M6 9v6M18 11c0 4-6 3-6 7"/></svg>Git branches</h3><table id="tBranch"><thead><tr>
<th>Branch</th><th class="num">Msgs</th><th class="num">Out</th><th class="num">LoC</th></tr></thead><tbody></tbody></table></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Longest prompts</h3><ul class="plist" id="lPrompts"></ul></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>Most-edited files<span class="hint">by LoC written</span></h3><table id="tFiles"><thead><tr>
<th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table></div>
</div>
</div>

<div id="viewWeek" style="display:none">
<div class="card wide" style="margin-bottom:16px">
<div class="wknav">
<button class="btn" id="wPrev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>Prev</button>
<h2 id="wTitle">…</h2>
<button class="btn" id="wNext">Next<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></button>
<span class="sp"></span>
<button class="btn act" id="wCopy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy as Markdown</button>
<button class="btn act" id="wAI"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>AI summary</button>
</div>
<div class="grid" id="wKpis" style="margin-bottom:0"></div>
</div>
<div class="cards">
<div class="card wide"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>What you worked on</h3><div class="worklist" id="wWork"></div></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg>Exploration vs building</h3><canvas id="cSplit"></canvas></div>
<div class="card"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8"/><rect x="14" y="6" width="3" height="12"/></svg>This week, day by day</h3><canvas id="cWeekDays"></canvas></div>
<div class="card wide"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></svg>AI Fluency report<span class="hint">delegation &middot; description &middot; discernment &middot; diligence</span></h3>
<div class="flgrid"><canvas id="cFluency" style="max-height:270px"></canvas><div id="flList"></div></div>
<div class="note">Heuristic scores computed locally from your logs, adapted for coding from Anthropic's 4D AI-fluency framework. Formulas in <code>docs/METRICS.md</code>.</div></div>
<div class="card wide"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.8.7 1 1.5 1 2.5h6c0-1 .2-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg>Reflection<span class="hint">a question this week's data raises</span></h3>
<div class="reflq" id="reflQ">&hellip;</div>
<button class="btn act" id="reflBtn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Discuss with Claude</button>
<div class="aiout" id="reflOut" style="margin-top:14px"></div>
<div class="note">Discussing runs <code>claude -p</code> locally — like the AI summary, it costs tokens only when you press the button.</div></div>
<div class="card wide" id="wAIcard" style="display:none"><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>AI-written summary</h3>
<div class="aiout" id="wAItext"></div>
<div class="note">Generated locally via <code>claude -p</code> — this is the only dashboard feature that consumes tokens, and only when you press the button.</div></div>
</div>
</div>

<div id="modal"><div class="mbox">
<button id="mClose">&times;</button>
<h2 id="mTitle"></h2>
<div class="msub" id="mSub"></div>
<canvas id="cProj" style="max-height:220px"></canvas>
<h3>Sessions</h3><ul class="plist" id="mSess"></ul>
<h3>Top files</h3><table id="mFiles"><thead><tr><th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table>
</div></div>

<footer>100% local &middot; reads Claude Code logs from <code id="root">~/.claude/projects</code> &middot; auto-refreshes every 15s. Costs are estimates at public API pricing (incl. cache read/write rates) — on a subscription plan the real marginal cost is $0. Edit the PRICING table in the script to tune.</footer>
</div>
<script>
const fmt=n=>n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':''+n;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtD=s=>{const p=s.split('-');return MON[+p[1]-1]+' '+(+p[2]);};
const fmtMin=m=>m>=60?Math.floor(m/60)+'h'+(m%60?' '+m%60+'m':''):m+'m';
const TT=['feature','bugfix','refactor','docs','explore','other'];
let days=30,charts={},D=null,wIdx=null,view='dash',customFrom='',customTo='';

document.getElementById('range').onclick=e=>{if(e.target.dataset.d===undefined)return;
days=+e.target.dataset.d;customFrom='';customTo='';
document.getElementById('cFrom').value='';document.getElementById('cTo').value='';
[...e.target.parentNode.children].forEach(b=>b.classList.remove('on'));
e.target.classList.add('on');load();};
document.getElementById('cApply').onclick=()=>{
const f=document.getElementById('cFrom').value,t=document.getElementById('cTo').value;
if(!f&&!t)return;customFrom=f;customTo=t;
[...document.getElementById('range').children].forEach(b=>b.classList.remove('on'));load();};
document.getElementById('tabDash').onclick=()=>setView('dash');
document.getElementById('tabWeek').onclick=()=>setView('week');
function setView(v){view=v;
document.getElementById('viewDash').style.display=v==='dash'?'':'none';
document.getElementById('viewWeek').style.display=v==='week'?'':'none';
document.getElementById('tabDash').classList.toggle('on',v==='dash');
document.getElementById('tabWeek').classList.toggle('on',v==='week');
if(D)render();}
function mk(id,cfg){if(charts[id])charts[id].destroy();charts[id]=new Chart(document.getElementById(id),cfg);}
const C={sky:'#56a3f5',emerald:'#34d399',violet:'#a78bfa',amber:'#fbbf24',rose:'#fb7185',teal:'#2dd4bf',coral:'#e8916b',slate:'#2c333f'};
const PIE=[C.coral,C.sky,C.violet,C.emerald,C.amber,C.teal,C.rose];
function areaFill(hex){return ctx=>{const ch=ctx.chart,a=ch.chartArea;if(!a)return hex+'22';
const g=ch.ctx.createLinearGradient(0,a.top,0,a.bottom);g.addColorStop(0,hex+'59');g.addColorStop(1,hex+'03');return g;};}
const GRID={grid:{color:'rgba(255,255,255,.05)',drawTicks:false},border:{display:false},ticks:{padding:8}};
const XGRID={grid:{display:false},border:{display:false},ticks:{padding:6,maxRotation:0,autoSkip:true,maxTicksLimit:12}};
Chart.defaults.font.family="'Segoe UI',-apple-system,Roboto,sans-serif";
Chart.defaults.font.size=11.5;Chart.defaults.color='#7c8798';Chart.defaults.borderColor='rgba(255,255,255,.06)';
Chart.defaults.plugins.legend.labels.usePointStyle=true;Chart.defaults.plugins.legend.labels.boxWidth=8;
Chart.defaults.plugins.legend.labels.boxHeight=8;Chart.defaults.plugins.legend.labels.padding=15;
Chart.defaults.plugins.tooltip.backgroundColor='rgba(16,20,28,.97)';Chart.defaults.plugins.tooltip.borderColor='rgba(255,255,255,.1)';
Chart.defaults.plugins.tooltip.borderWidth=1;Chart.defaults.plugins.tooltip.padding=11;Chart.defaults.plugins.tooltip.cornerRadius=9;
Chart.defaults.plugins.tooltip.titleColor='#e7edf5';Chart.defaults.plugins.tooltip.bodyColor='#aab4c4';
Chart.defaults.plugins.tooltip.usePointStyle=true;Chart.defaults.plugins.tooltip.boxPadding=5;
Chart.defaults.elements.bar.borderRadius=5;Chart.defaults.elements.bar.borderSkipped=false;
Chart.defaults.elements.point.radius=0;Chart.defaults.elements.point.hoverRadius=5;Chart.defaults.elements.point.hitRadius=12;
Chart.defaults.elements.line.tension=.35;Chart.defaults.elements.line.borderWidth=2;
Chart.defaults.maintainAspectRatio=false;
const IC={in:'<path d="M12 3v13m0 0 4-4m-4 4-4-4M4 21h16"/>',out:'<path d="M12 21V8m0 0 4 4m-4-4-4 4M4 3h16"/>',
cache:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
cost:'<path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
code:'<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',prompt:'<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
session:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
alert:'<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
user:'<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
lever:'<path d="M23 6l-9.5 9.5-5-5L1 18"/><path d="M17 6h6v6"/>'};
const ic=k=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${IC[k]}</svg>`;

async function load(){
const qs=(customFrom||customTo)?('from='+encodeURIComponent(customFrom)+'&to='+encodeURIComponent(customTo)):('days='+days);
const r=await fetch('/api/stats?'+qs);D=await r.json();
document.getElementById('upd').textContent=D.generated;
document.getElementById('root').textContent=D.root||'~/.claude/projects';
var pw=document.getElementById('parsewarn');
if(D.totals.parse_errors>0){if(!pw){pw=document.createElement('div');pw.id='parsewarn';pw.className='parsewarn';document.getElementById('alerts').appendChild(pw);}pw.innerHTML='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg><span>'+D.totals.parse_errors.toLocaleString()+' log entries could not be parsed \u2014 the log format may have changed; stats may be incomplete.</span>';}else if(pw){pw.remove();}
if(wIdx===null&&D.weeks.length)wIdx=D.weeks.length-1;
if(wIdx!==null&&wIdx>=D.weeks.length)wIdx=D.weeks.length-1;
render();}

function render(){if(view==='dash')renderDash();else renderWeek();}

function renderMomentum(){
const card=document.getElementById('momCard');
if(D.streak===undefined){card.style.display='none';return;}
card.style.display='';
const g=D.goals||{},today=D.today||{};
const flame='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2s5 4.5 5 9a5 5 0 0 1-10 0c0-1.2.4-2.3.4-2.3S5 10 5 13a7 7 0 0 0 14 0c0-5.5-7-11-7-11z"/></svg>';
const cal='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>';
let h=`<div class="mblock"><div class="mico">${flame}</div><div><div class="mv">${D.streak}</div><div class="ml">day streak &middot; best ${D.best_streak}</div></div></div>`;
h+=`<div class="sep"></div><div class="mblock"><div class="mico">${cal}</div><div><div class="mv">${D.active_days}</div><div class="ml">active days all-time</div></div></div>`;
const bars=[];
function bar(label,val,goal){if(!goal)return;const pc=Math.min(100,Math.round(val/goal*100)),over=val>goal;
bars.push(`<div class="goalw"><div class="gt"><span>${label}</span><b>${fmt(val)} / ${fmt(goal)}</b></div><div class="bar${over?' over':''}"><i style="width:${pc}%"></i></div></div>`);}
bar("Today's LoC",today.loc||0,g.daily_loc);
bar("Today's output",today.tokens||0,g.daily_tokens);
bar('This week output',D.week_tokens||0,g.weekly_tokens);
if(bars.length)h+=`<div class="goals">${bars.join('')}</div>`;
document.getElementById('mom').innerHTML=h;}

function renderDash(){
const d=D,t=d.totals;
renderMomentum();
const kpis=[
['Input tokens',fmt(t.input),'sent to Claude','in',C.sky,t.input,'input'],
['Output tokens',fmt(t.output),'generated by Claude','out',C.emerald,t.output,'output'],
['Cache read',fmt(t.cache_read),t.cache_efficiency+'% cache hit rate','cache',C.teal,t.cache_read,'cache_read'],
['Est. API cost','$'+t.cost,'$0 on a subscription plan','cost',C.amber,t.cost,'cost'],
['Lines of code',fmt(t.loc),'written by Claude','code',C.violet,t.loc,'loc'],
['Prompts',fmt(t.prompts),t.avg_prompt_lines+' lines avg','prompt',C.coral,t.prompts,'prompts'],
['Sessions',t.sessions,t.avg_session_min+' min avg','session',C.sky,t.sessions,'sessions'],
['Friction',t.interruptions,t.tool_errors+' tool errors','alert',C.rose],
['Your tokens',fmt(t.human_tokens),'typed in prompts (est.)','user',C.amber,t.human_tokens,'human_tokens'],
['Leverage',t.leverage+'×','Claude tokens per typed token','lever',C.emerald]];
document.getElementById('kpis').innerHTML=kpis.map(k=>{
const dh=(k[6]&&D.prev)?delta(k[5],D.prev[k[6]]):'';
return `<div class="kpi" style="--kc:${k[4]};--kbg:color-mix(in srgb, ${k[4]} 16%, transparent)"><div class="khead"><span class="l">${k[0]}</span><span class="ic">${ic(k[3])}</span></div><div class="v">${k[1]}</div>${dh?`<div class="kd">${dh}</div>`:''}<div class="s">${k[2]}</div></div>`;}).join('');
const labels=d.days.map(x=>x.day.slice(5));
mk('cTokens',{type:'bar',data:{labels,datasets:[
{label:'Input',data:d.days.map(x=>x.input),backgroundColor:C.sky},
{label:'Output',data:d.days.map(x=>x.output),backgroundColor:C.emerald},
{label:'Cache read',data:d.days.map(x=>x.cache_read),backgroundColor:C.slate}]},
options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},plugins:{legend:{position:'bottom'}}}});
mk('cBalance',{type:'line',data:{labels,datasets:[
{label:'You (typed)',data:d.days.map(x=>x.human_tokens),borderColor:C.amber,backgroundColor:areaFill(C.amber),fill:true},
{label:'Claude (output)',data:d.days.map(x=>x.output),borderColor:C.coral,backgroundColor:areaFill(C.coral),fill:true}]},
options:{scales:{x:XGRID,y:{type:'logarithmic',...GRID}},plugins:{legend:{position:'bottom'}}}});
mk('cModels',{type:'doughnut',data:{labels:d.models.map(m=>m.name),
datasets:[{data:d.models.map(m=>m.output),backgroundColor:PIE,borderColor:'#0e1117',borderWidth:2,hoverOffset:6}]},
options:{cutout:'66%',plugins:{legend:{position:'bottom'}}}});
mk('cLoc',{type:'line',data:{labels,datasets:[{label:'LoC',data:d.days.map(x=>x.loc),
borderColor:C.violet,backgroundColor:areaFill(C.violet),fill:true}]},
options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false}}}});
mk('cRW',{type:'bar',data:{labels,datasets:[
{label:'Exploration (reads/searches)',data:d.days.map(x=>x.reads),backgroundColor:C.sky},
{label:'Building (file edits)',data:d.days.map(x=>x.writes),backgroundColor:C.emerald}]},
options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},plugins:{legend:{position:'bottom'}}}});
mk('cFrict',{type:'bar',data:{labels,datasets:[
{label:'Interruptions',data:d.days.map(x=>x.interruptions),backgroundColor:C.rose},
{label:'Tool errors',data:d.days.map(x=>x.tool_errors),backgroundColor:C.amber}]},
options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},plugins:{legend:{position:'bottom'}}}});
// --- time & wellness -------------------------------------------------------
const actTot=d.days.reduce((a,x)=>a+(x.active_min||0),0);
const msgTot=d.days.reduce((a,x)=>a+x.messages,0);
const lateTot=d.days.reduce((a,x)=>a+(x.late_msgs||0),0);
const focusMax=d.days.reduce((a,x)=>Math.max(a,x.focus_max||0),0);
const wkndMsgs=d.days.reduce((a,x)=>{const g=new Date(x.day+'T12:00:00').getDay();return a+((g===0||g===6)?x.messages:0);},0);
const actDays=d.days.filter(x=>x.messages>0).length;
const wf=[[fmtMin(actTot),'active time in range'],
[actDays?fmtMin(Math.round(actTot/actDays)):'0m','avg per active day'],
[fmtMin(focusMax),focusMax>=180?'longest focus block — take breaks!':'longest focus block'],
[(msgTot?Math.round(lateTot/msgTot*100):0)+'%','in quiet hours (23–06)'],
[(msgTot?Math.round(wkndMsgs/msgTot*100):0)+'%','on weekends']];
document.getElementById('wellFacts').innerHTML=wf.map(f=>`<div class="f"><div class="fv">${f[0]}</div><div class="fl">${f[1]}</div></div>`).join('');
mk('cActive',{type:'bar',data:{labels,datasets:[{label:'Active minutes',data:d.days.map(x=>x.active_min||0),
backgroundColor:d.days.map(x=>((x.late_msgs||0)>x.messages*.25&&x.messages)?C.rose:C.teal)}]},
options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>fmtMin(c.parsed.y)+' active'+((d.days[c.dataIndex].late_msgs||0)>d.days[c.dataIndex].messages*.25?' · heavy quiet-hours use':'')}}}}});
// --- task mix --------------------------------------------------------------
const byDay={};d.days.forEach(x=>byDay[x.day]=Object.fromEntries(TT.map(t=>[t,0])));
(D.sessions_list||[]).forEach(s=>{if(byDay[s.day]&&s.type)byDay[s.day][s.type]++;});
const TC={feature:C.emerald,bugfix:C.rose,refactor:C.violet,docs:C.amber,explore:C.sky,other:C.slate};
mk('cTaskMix',{type:'bar',data:{labels,datasets:TT.map(t=>({label:t,data:d.days.map(x=>byDay[x.day][t]),backgroundColor:TC[t]}))},
options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID,ticks:{precision:0}}},plugins:{legend:{position:'bottom'}}}});
mk('cTools',{type:'bar',data:{labels:d.tools.map(x=>x[0]),datasets:[{data:d.tools.map(x=>x[1]),
backgroundColor:C.coral,borderRadius:4}]},options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
mk('cSlash',{type:'bar',data:{labels:d.slash.map(x=>x[0]),datasets:[{data:d.slash.map(x=>x[1]),
backgroundColor:C.teal,borderRadius:4}]},options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
const wd=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];const max=Math.max(1,...d.heatmap.flat());
let hm='<div></div>'+[...Array(24).keys()].map(h=>`<div class="hh">${h%2?'':h}</div>`).join('');
d.heatmap.forEach((row,i)=>{hm+=`<div class="wd">${wd[i]}</div>`+row.map((v,h)=>
`<div class="cell" title="${wd[i]} ${h}:00 — ${v} message${v===1?'':'s'}" style="background:${v?`rgba(232,145,107,${.15+.8*v/max})`:'rgba(255,255,255,.03)'}"></div>`).join('');});
document.getElementById('heat').innerHTML=hm;
document.querySelector('#tProj tbody').innerHTML=d.projects.map(p=>
`<tr><td title="${esc(p.name)}"><b>${esc(p.name.split('/').pop()||p.name)}</b></td><td class="num">${fmt(p.input)}</td><td class="num">${fmt(p.output)}</td><td class="num">${fmt(p.loc)}</td><td class="num">${p.cost}</td></tr>`).join('');
document.querySelectorAll('#tProj tbody tr').forEach((tr,i)=>tr.onclick=()=>openProject(d.projects[i]));
document.querySelector('#tModels tbody').innerHTML=d.models.map(m=>
`<tr><td><b>${esc(m.name)}</b></td><td class="num">${m.msgs}</td><td class="num">${fmt(m.input)}</td><td class="num">${fmt(m.output)}</td><td class="num">${m.cost}</td></tr>`).join('');
document.querySelector('#tBranch tbody').innerHTML=d.branches.map(b=>
`<tr><td><b>${esc(b.name)}</b></td><td class="num">${b.msgs}</td><td class="num">${fmt(b.output)}</td><td class="num">${fmt(b.loc)}</td></tr>`).join('');
document.getElementById('lPrompts').innerHTML=d.longest_prompts.map(p=>
`<li>${esc(p.preview)}…<div class="mt"><b>${p.lines} lines</b> · ${p.words} words · ${p.day}</div></li>`).join('');
document.querySelector('#tFiles tbody').innerHTML=d.top_files.map(f=>
`<tr><td>${esc(f[0])}</td><td class="num">${fmt(f[1])}</td></tr>`).join('');
}

// ---------------------------------------------------------------- weekly tab
const delta=(c,p)=>{if(p==null||!isFinite(c/p)||p===0)return'';
const pc=Math.round((c-p)/p*100);if(pc===0)return'<span style="color:var(--dim)">= last wk</span>';
const up=pc>0;return `<span style="color:${up?'var(--green)':'var(--red)'}">${up?'&#9650;':'&#9660;'} ${Math.abs(pc)}% vs last wk</span>`;};

function buildPct(w){const t=w.reads+w.writes;return t?Math.round(w.writes/t*100):0;}

// --- AI fluency (4D) heuristics --------------------------------------------
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

function fluencyTips(f,w){
return{
delegation:f.delegation<50?'Try handing Claude whole tasks (implement + verify), not just questions.':'Healthy mix of asking and delegating real work.',
description:f.description<50?'Front-load constraints, file paths and expected behavior in the first prompt.':'Most prompts land without needing a correction.',
discernment:f.engaged<.3?'You rarely push back — spot-check diffs and say so when output misses.':(f.discernment<50?'High error rate — slow down and review output before continuing.':'You actively steer and catch issues.'),
diligence:f.ncode===0?'No code sessions this week.':(f.diligence<50?'Ask Claude to run tests/commands after edits — most code sessions never verified.':'Most code sessions verified their work with commands.')};}

function renderFluency(w,prev){
const f=fluencyOf(w),tips=fluencyTips(f,w),fp=prev?fluencyOf(prev):null;
const dims=[
['Delegation',f.delegation,`<b>${Math.round(f.codeShare*100)}%</b> of sessions shipped code · <b>${f.lev.toFixed(1)}×</b> leverage`],
['Description',f.description,`<b>${Math.round(f.oneShot*100)}%</b> of prompts needed no correction · <b>${Math.round(f.avgW)}</b> words avg`],
['Discernment',f.discernment,`<b>${w.corrections||0}</b> corrections + <b>${w.interruptions}</b> interruptions · <b>${w.tool_errors}</b> tool errors`],
['Diligence',f.diligence,`<b>${Math.round(f.verified*100)}%</b> of ${f.ncode} code session${f.ncode===1?'':'s'} ran commands after edits`]];
const key=['delegation','description','discernment','diligence'];
document.getElementById('flList').innerHTML=dims.map((d,i)=>
`<div class="flrow"><span class="fln">${d[0]}</span><span class="flb"><span class="bar"><i style="width:${d[1]}%"></i></span></span><span class="fls">${d[1]}</span><span class="flt">${d[2]}<br>${tips[key[i]]}</span></div>`).join('');
const ds=[{label:'This week',data:dims.map(d=>d[1]),borderColor:C.coral,backgroundColor:'rgba(232,145,107,.16)',pointBackgroundColor:C.coral,borderWidth:2}];
if(fp)ds.push({label:'Last week',data:[fp.delegation,fp.description,fp.discernment,fp.diligence],borderColor:C.sky,backgroundColor:'rgba(86,163,245,.06)',pointBackgroundColor:C.sky,borderWidth:1.5});
mk('cFluency',{type:'radar',data:{labels:dims.map(d=>d[0]),datasets:ds},
options:{scales:{r:{min:0,max:100,ticks:{display:false,stepSize:25},grid:{color:'rgba(255,255,255,.08)'},angleLines:{color:'rgba(255,255,255,.08)'},pointLabels:{color:'#aab4c4',font:{size:12}}}},plugins:{legend:{position:'bottom'}}}});}

// --- reflection question ----------------------------------------------------
let reflQtext='';
function pickReflection(w,idx){
const c=[];
const corrRate=w.prompts?(w.corrections||0)/w.prompts:0;
if(corrRate>0.08)c.push(`You corrected Claude ${w.corrections} times this week (${Math.round(corrRate*100)}% of prompts). What context could your first prompts include so the second try isn't needed?`);
const late=w.messages?(w.late_msgs||0)/w.messages:0;
if(late>0.2)c.push(`${Math.round(late*100)}% of this week's activity happened between 11pm and 6am. Is late-night coding a deliberate choice — or a habit worth questioning?`);
const tot=w.reads+w.writes,ex=tot?w.reads/tot:0;
if(tot&&ex>0.65)c.push(`${Math.round(ex*100)}% of tool calls this week were exploration. Are you using Claude mostly to understand code — and is there building you could delegate too?`);
if(tot&&ex<0.15)c.push(`${Math.round((1-ex)*100)}% of tool calls this week were edits. Are you reading and verifying what gets written — or shipping on trust?`);
const code=w.sessions.filter(s=>s.loc>0),ver=code.length?code.filter(s=>s.bash>0).length/code.length:1;
if(code.length>2&&ver<0.4)c.push(`Only ${Math.round(ver*100)}% of code-writing sessions ran a command afterwards. How do you know this week's ${fmt(w.loc)} new lines actually work?`);
c.push(`What's one thing you want to keep doing yourself, even if Claude could do it faster?`);
return c[idx%c.length];}

function renderWeek(){
const wk=D.weeks;
if(!wk.length){document.getElementById('wTitle').textContent='No data yet';return;}
if(wIdx===null)wIdx=wk.length-1;
const w=wk[wIdx],prev=wIdx>0?wk[wIdx-1]:null;
document.getElementById('wPrev').disabled=wIdx===0;
document.getElementById('wNext').disabled=wIdx===wk.length-1;
document.getElementById('wTitle').textContent=`Week of ${fmtD(w.start)} – ${fmtD(w.end)}`;
const bp=buildPct(w);
const kpis=[
['Sessions',w.sessions.length,delta(w.sessions.length,prev&&prev.sessions.length)],
['Focus hours',(w.minutes/60).toFixed(1),delta(w.minutes,prev&&prev.minutes)],
['Output tokens',fmt(w.output),delta(w.output,prev&&prev.output)],
['Lines of code',fmt(w.loc),delta(w.loc,prev&&prev.loc)],
['Prompts',fmt(w.prompts),delta(w.prompts,prev&&prev.prompts)],
['Building',bp+'%',(100-bp)+'% exploration']];
document.getElementById('wKpis').innerHTML=kpis.map(k=>
`<div class="kpi"><div class="khead"><span class="l">${k[0]}</span></div><div class="v">${k[1]}</div><div class="s">${k[2]||''}</div></div>`).join('');
const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
document.getElementById('wWork').innerHTML=Object.keys(g).length?Object.keys(g).map(p=>
`<h4>${esc(p)}</h4><ul>`+g[p].map(s=>
`<li>${esc(s.title)}<br><span class="meta">${fmtD(s.day)} · ${s.min} min · ${fmt(s.loc)} LoC · ${s.prompts} prompts</span></li>`).join('')+'</ul>').join('')
:'<span style="color:var(--dim)">No sessions logged this week.</span>';
mk('cSplit',{type:'doughnut',data:{labels:['Building (file edits)','Exploration (reads/searches)'],
datasets:[{data:[w.writes,w.reads],backgroundColor:[C.emerald,C.sky],borderColor:'#0e1117',borderWidth:2,hoverOffset:6}]},
options:{cutout:'66%',plugins:{legend:{position:'bottom'}}}});
mk('cWeekDays',{type:'bar',data:{labels:w.days.map(x=>fmtD(x.day)),datasets:[
{label:'Output tokens',data:w.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
{label:'LoC',data:w.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
options:{scales:{x:XGRID,y:{position:'left',...GRID},y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
plugins:{legend:{position:'bottom'}}}});
renderFluency(w,prev);
reflQtext=pickReflection(w,wIdx);
document.getElementById('reflQ').textContent=reflQtext;
document.getElementById('reflOut').textContent='';
}

document.getElementById('wPrev').onclick=()=>{if(wIdx>0){wIdx--;renderWeek();}};
document.getElementById('wNext').onclick=()=>{if(wIdx<D.weeks.length-1){wIdx++;renderWeek();}};

document.getElementById('reflBtn').onclick=async()=>{
if(!D||!D.weeks.length||!reflQtext)return;
const btn=document.getElementById('reflBtn'),out=document.getElementById('reflOut');
btn.disabled=true;btn.textContent='Thinking…';
out.textContent='Running claude -p — this can take a minute…';
try{const r=await fetch('/api/reflect?week='+encodeURIComponent(D.weeks[wIdx].key)+'&q='+encodeURIComponent(reflQtext));
const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
catch(e){out.textContent='Failed: '+e;}
btn.disabled=false;btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Discuss with Claude';};

function weekMarkdown(w){
const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
let md=`# Weekly Update — ${fmtD(w.start)} to ${fmtD(w.end)}\n\n## What I worked on\n`;
for(const p in g){md+=`\n### ${p}\n`;g[p].forEach(s=>{md+=`- ${s.title} (${s.day}, ~${s.min} min)\n`;});}
const bp=buildPct(w);
md+=`\n## Numbers\n`;
md+=`- ${w.sessions.length} sessions, ~${(w.minutes/60).toFixed(1)} focus-hours\n`;
md+=`- ${w.loc.toLocaleString()} lines of code written with Claude\n`;
md+=`- ${w.prompts.toLocaleString()} prompts · ${w.output.toLocaleString()} tokens generated\n`;
md+=`- Work split: ${bp}% building / ${100-bp}% exploration\n`;
const tc={};w.sessions.forEach(s=>{const t=s.type||'other';tc[t]=(tc[t]||0)+1});
const mix=TT.filter(t=>tc[t]).map(t=>`${tc[t]} ${t}`).join(', ');
if(mix)md+=`- Task mix: ${mix}\n`;
return md;}

document.getElementById('wCopy').onclick=async()=>{
if(!D||!D.weeks.length)return;
const btn=document.getElementById('wCopy');
try{await navigator.clipboard.writeText(weekMarkdown(D.weeks[wIdx]));btn.textContent='✔ Copied!';}
catch(e){const ta=document.createElement('textarea');ta.value=weekMarkdown(D.weeks[wIdx]);
document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();btn.textContent='✔ Copied!';}
setTimeout(()=>btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy as Markdown',2000);};

document.getElementById('wAI').onclick=async()=>{
if(!D||!D.weeks.length)return;
const btn=document.getElementById('wAI'),card=document.getElementById('wAIcard'),out=document.getElementById('wAItext');
btn.disabled=true;btn.textContent='Generating…';card.style.display='';
out.textContent='Running claude -p — this can take a minute…';
try{const r=await fetch('/api/ai_summary?week='+encodeURIComponent(D.weeks[wIdx].key));
const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
catch(e){out.textContent='Failed: '+e;}
btn.disabled=false;btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>AI summary';};

// ---------------------------------------------------------- project deep-dive
function openProject(p){
document.getElementById('mTitle').textContent=p.name.split('/').pop()||p.name;
document.getElementById('mSub').innerHTML=`${esc(p.name)} &middot; ${fmt(p.input)} in &middot; ${fmt(p.output)} out &middot; ${fmt(p.loc)} LoC &middot; ${p.sessions} sessions &middot; est. $${p.cost}`;
document.getElementById('modal').style.display='flex';
mk('cProj',{type:'bar',data:{labels:p.days.map(x=>x.day.slice(5)),datasets:[
{label:'Output tokens',data:p.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
{label:'LoC',data:p.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
options:{scales:{x:XGRID,y:{position:'left',...GRID},y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
plugins:{legend:{position:'bottom'}}}});
const sess=D.sessions_list.filter(s=>s.project===p.name);
document.getElementById('mSess').innerHTML=sess.length?sess.map(s=>
`<li><b>${esc(s.title)}</b><div class="mt">${s.day} · ${s.min} min · ${fmt(s.loc)} LoC · ${s.prompts} prompts · ${fmt(s.output)} tokens out</div></li>`).join('')
:'<li>No session details available.</li>';
document.querySelector('#mFiles tbody').innerHTML=(p.files||[]).map(f=>
`<tr><td>${esc(f[0])}</td><td class="num">${fmt(f[1])}</td></tr>`).join('');}
document.getElementById('mClose').onclick=()=>document.getElementById('modal').style.display='none';
document.getElementById('modal').onclick=e=>{if(e.target.id==='modal')e.target.style.display='none';};

// ----------------------------------------------------------------- exports
function download(name,text,type){const b=new Blob([text],{type});const u=URL.createObjectURL(b);
const a=document.createElement('a');a.href=u;a.download=name;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u);}
function rangeLabel(){return (customFrom||customTo)?((customFrom||'start')+'_to_'+(customTo||'now')):(days?('last'+days+'d'):'alltime');}
document.getElementById('expJson').onclick=()=>{if(D)download('claude-usage_'+rangeLabel()+'.json',JSON.stringify(D,null,2),'application/json');};
document.getElementById('expCsv').onclick=()=>{if(!D)return;
const rows=[['date','input','output','cache_read','cache_write','loc','prompts','reads','writes','est_cost_usd']];
D.days.forEach(x=>rows.push([x.day,x.input,x.output,x.cache_read,x.cache_write,x.loc,x.prompts,x.reads,x.writes,x.cost]));
download('claude-usage_'+rangeLabel()+'.csv',rows.map(r=>r.join(',')).join('\n'),'text/csv');};
document.getElementById('expCopy').onclick=async()=>{if(!D)return;const t=D.totals,btn=document.getElementById('expCopy');
const txt=`Claude Code usage — ${rangeLabel().replace(/_/g,' ')}\n`+
`Output tokens: ${t.output.toLocaleString()}\nInput tokens: ${t.input.toLocaleString()}\n`+
`Cache read: ${t.cache_read.toLocaleString()} (${t.cache_efficiency}% hit)\n`+
`Lines of code: ${t.loc.toLocaleString()}\nPrompts: ${t.prompts.toLocaleString()}\n`+
`Sessions: ${t.sessions} (${t.avg_session_min} min avg)\nEst. API cost: $${t.cost}\nLeverage: ${t.leverage}x`;
try{await navigator.clipboard.writeText(txt);}catch(e){const ta=document.createElement('textarea');ta.value=txt;
document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}
const old=btn.innerHTML;btn.textContent='✔ Copied';setTimeout(()=>btn.innerHTML=old,1600);};

load();setInterval(load,15000);
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
                                ("output", "input", "loc", "prompts", "human_tokens", "cache_read")}
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
    args = ap.parse_args()

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

    Handler.scanner = scanner
    Handler.root = args.dir
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"  Dev Token Dashboard running at {url}")
    print(f"  Reading logs from: {args.dir}")
    print("  Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye!")


if __name__ == "__main__":
    main()
