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

CODE_TOOLS_WRITE = {"Write", "Create"}          # tools whose 'content' is new code
CODE_TOOLS_EDIT = {"Edit", "StrEditReplace"}    # tools whose 'new_string' is new code

# tool classification for the exploration-vs-building split
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
              "NotebookRead", "TodoRead", "ToolSearch"}
WRITE_TOOLS = CODE_TOOLS_WRITE | CODE_TOOLS_EDIT | {"MultiEdit", "NotebookEdit"}

CMD_RE = re.compile(r"<command-name>\s*(/?[\w:_-]+)\s*</command-name>")


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
        "messages": 0, "human_tokens": 0,
        "reads": 0, "writes": 0, "interruptions": 0, "tool_errors": 0,
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
        })
        if ts:
            if not s["start"] or ts < s["start"]:
                s["start"] = ts
            if not s["end"] or ts > s["end"]:
                s["end"] = ts
        s["msgs"] += 1
        return s


def parse_entry(entry: dict, project: str, st: Stats):
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

    sid = entry.get("sessionId")
    branch = entry.get("gitBranch") or "(none)"
    sess = st.touch_session(sid, ts_raw, project)
    if day:
        st.days[day]["messages"] += 1
    if hour is not None:
        st.heatmap[(weekday, hour)] += 1

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
        if day:
            st.days[day]["prompts"] += 1
            st.days[day]["prompt_lines"] += lines
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
        del st.longest_prompts[8:]
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

    def build_stats(self) -> Stats:
        with self._lock:
            self.scan()
            st = Stats()
            for path, entries in self._entries.items():
                project = os.path.basename(os.path.dirname(path)) or "unknown"
                # prettify: Claude Code encodes the cwd in the dir name
                project = project.lstrip("-").replace("--", "/").replace("-", "/")
                for e in entries:
                    try:
                        parse_entry(e, project, st)
                    except Exception:
                        st.parse_errors += 1
            if st.parse_errors:
                print(f"[warn] {st.parse_errors} log entries could not be parsed "
                      "(log format may have changed - stats may be incomplete)")
            return st


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
            "days": [], "sessions": [],
        })
        for f in ("input", "output", "loc", "prompts", "reads", "writes",
                  "interruptions", "tool_errors", "human_tokens"):
            w[f] += ds[f]
        w["cost"] += ds["cost"]
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
        weeks[key]["sessions"].append({
            "id": sid[:8],
            "title": st.session_titles.get(sid) or "Untitled session",
            "project": s["project"], "day": a.strftime("%Y-%m-%d"),
            "min": round(mins), "loc": s["loc"], "reads": s["reads"],
            "writes": s["writes"], "output": s["output"], "prompts": s["prompts"],
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


def stats_to_json(st: Stats, days_filter=None) -> dict:
    all_days = sorted(st.days.keys())
    if days_filter:
        all_days = all_days[-days_filter:]
    dayset = set(all_days)

    def tot(key):
        return sum(st.days[d][key] for d in all_days)

    prompts_total = tot("prompts")
    sessions = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00"))
            dur = max(0, (b - a).total_seconds())
        except Exception:
            dur = 0
        sessions.append(dur)
    avg_session_min = (sum(sessions) / len(sessions) / 60) if sessions else 0

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
        sessions_list.append({
            "id": sid[:8], "title": st.session_titles.get(sid) or "Untitled session",
            "project": s["project"], "day": day, "min": mins,
            "loc": s["loc"], "reads": s["reads"], "writes": s["writes"],
            "output": s["output"], "prompts": s["prompts"],
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
            "sessions": len(st.sessions),
            "avg_session_min": round(avg_session_min, 1),
            "cache_efficiency": round(cache_eff, 1),
            "avg_prompt_lines": round(tot("prompt_lines") / prompts_total, 1) if prompts_total else 0,
            "avg_prompt_words": round(st.prompt_words / prompts_total, 1) if prompts_total else 0,
            "interruptions": tot("interruptions"),
            "tool_errors": tot("tool_errors"),
            "reads": tot("reads"), "writes": tot("writes"),
            "human_tokens": tot("human_tokens"),
            "leverage": round(tot("output") / tot("human_tokens"), 1) if tot("human_tokens") else 0,
            "parse_errors": st.parse_errors,
        },
        "days": [{"day": d, **st.days[d], "cost": round(st.days[d]["cost"], 3)} for d in all_days],
        "models": models,
        "tools": st.tools.most_common(12),
        "slash": st.slash.most_common(12),
        "projects": projects[:10],
        "branches": branches[:10],
        "heatmap": heat,
        "longest_prompts": [
            {"lines": l, "words": w, "preview": p, "day": d}
            for (l, w, p, d) in st.longest_prompts if d in dayset or not days_filter
        ],
        "top_files": st.files_touched.most_common(8),
        "sessions_list": sessions_list,
        # weekly report data is always computed over all history
        "weeks": build_weeks(st),
    }


# ---------------------------------------------------------------------------
# Optional AI weekly summary (runs `claude -p`; the ONE feature that costs
# tokens, triggered only by an explicit button press in the UI)
# ---------------------------------------------------------------------------

def ai_week_summary(week: dict) -> dict:
    import shutil
    import subprocess
    if not shutil.which("claude"):
        return {"ok": False, "error": "claude CLI not found on PATH."}
    bullets = "\n".join(
        "- [%s] %s (%s, %s min, %s LoC)" % (s["project"], s["title"], s["day"], s["min"], s["loc"])
        for s in week["sessions"]
    ) or "- (no sessions logged this week)"
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
    cmd = ["cmd", "/c", "claude", "-p"] if os.name == "nt" else ["claude", "-p"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=240, encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    text = (r.stdout or "").strip()
    if r.returncode != 0 or not text:
        return {"ok": False, "error": (r.stderr or "claude -p returned no output").strip()[:500]}
    return {"ok": True, "text": text}


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Dev Token Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="/chart.umd.min.js"></script>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--txt:#e6edf3;--dim:#8b949e;
--acc:#58a6ff;--green:#3fb950;--orange:#d29922;--purple:#bc8cff;--red:#f85149}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;padding:24px}
h1{font-size:22px;margin-bottom:4px}
.sub{color:var(--dim);margin-bottom:14px;font-size:13px}
.tabs{margin-bottom:16px}
.tabs button{background:var(--card);border:1px solid var(--border);color:var(--dim);
padding:7px 18px;border-radius:6px;cursor:pointer;margin-right:8px;font-size:14px}
.tabs button.on{color:var(--txt);border-color:var(--acc);background:#11253e}
.range{margin-bottom:18px}
.range button{background:var(--card);border:1px solid var(--border);color:var(--dim);
padding:5px 14px;border-radius:6px;cursor:pointer;margin-right:6px;font-size:13px}
.range button.on{color:var(--txt);border-color:var(--acc);background:#11253e}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.kpi .v{font-size:22px;font-weight:700;margin-top:2px}
.kpi .l{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.kpi .s{color:var(--dim);font-size:11px;margin-top:2px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.card h3{font-size:14px;margin-bottom:12px;color:var(--acc)}
.card.wide{grid-column:1/-1}
canvas{max-height:280px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
th{color:var(--dim);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
#tProj tbody tr{cursor:pointer}
#tProj tbody tr:hover{background:#1c2430}
.hm{display:grid;grid-template-columns:34px repeat(24,1fr);gap:2px;font-size:10px;color:var(--dim)}
.hm .cell{aspect-ratio:1;border-radius:2px;background:#1c2430}
.plist li{margin-bottom:8px;color:var(--dim);font-size:12.5px;list-style:none}
.plist b{color:var(--txt)}
footer{color:var(--dim);font-size:12px;margin-top:22px}
.wknav{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.wknav h2{font-size:17px}
.wknav .sp{flex:1}
.wknav button{background:var(--card);border:1px solid var(--border);color:var(--txt);
padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
.wknav button.act{border-color:var(--acc);color:var(--acc)}
.wknav button:disabled{opacity:.35;cursor:default}
.worklist h4{color:var(--purple);font-size:13px;margin:12px 0 6px}
.worklist ul{list-style:none}
.worklist li{padding:5px 0;border-bottom:1px solid var(--border);font-size:13px}
.worklist li .meta{color:var(--dim);font-size:12px}
.aiout{white-space:pre-wrap;font-size:13.5px;line-height:1.6}
.note{color:var(--dim);font-size:11.5px;margin-top:8px}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.65);display:none;z-index:10;
align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto}
#modal .mbox{background:var(--card);border:1px solid var(--border);border-radius:12px;
padding:22px;max-width:820px;width:100%;position:relative}
#modal h2{font-size:17px;margin-bottom:14px;color:var(--acc)}
#modal h3{font-size:13px;margin:16px 0 8px;color:var(--dim)}
#mClose{position:absolute;top:12px;right:14px;background:none;border:none;color:var(--dim);
font-size:22px;cursor:pointer}
</style></head><body>
<h1>&#9889; Dev Token Dashboard</h1>
<div class="sub">100% local &middot; reads Claude Code logs from <code id="root"></code> &middot; zero tokens consumed &middot; last updated <span id="upd">…</span> (auto-refreshes)</div>
<div class="tabs"><button id="tabDash" class="on">Dashboard</button><button id="tabWeek">Weekly Report</button></div>

<div id="viewDash">
<div class="range" id="range">
<button data-d="7">7 days</button><button data-d="30" class="on">30 days</button>
<button data-d="90">90 days</button><button data-d="0">All time</button></div>
<div class="grid" id="kpis"></div>
<div class="cards">
<div class="card wide"><h3>Daily tokens</h3><canvas id="cTokens"></canvas></div>
<div class="card wide"><h3>You vs Claude — tokens you typed vs tokens Claude produced (log scale)</h3><canvas id="cBalance"></canvas></div>
<div class="card"><h3>Model usage (output tokens)</h3><canvas id="cModels"></canvas></div>
<div class="card"><h3>Lines of code generated / day</h3><canvas id="cLoc"></canvas></div>
<div class="card"><h3>Exploration vs building / day (tool calls)</h3><canvas id="cRW"></canvas></div>
<div class="card"><h3>Friction / day (interruptions &amp; tool errors)</h3><canvas id="cFrict"></canvas></div>
<div class="card"><h3>Tool usage</h3><canvas id="cTools"></canvas></div>
<div class="card"><h3>Slash commands</h3><canvas id="cSlash"></canvas></div>
<div class="card wide"><h3>Activity heatmap (messages by weekday &times; hour)</h3><div class="hm" id="heat"></div></div>
<div class="card"><h3>Projects <span style="color:var(--dim);font-weight:400;font-size:12px">(click a row for details)</span></h3><table id="tProj"><thead><tr>
<th>Project</th><th class="num">In</th><th class="num">Out</th><th class="num">LoC</th><th class="num">Est. $</th></tr></thead><tbody></tbody></table></div>
<div class="card"><h3>Model breakdown</h3><table id="tModels"><thead><tr>
<th>Model</th><th class="num">Msgs</th><th class="num">In</th><th class="num">Out</th><th class="num">Est. $</th></tr></thead><tbody></tbody></table></div>
<div class="card"><h3>Git branches</h3><table id="tBranch"><thead><tr>
<th>Branch</th><th class="num">Msgs</th><th class="num">Out</th><th class="num">LoC</th></tr></thead><tbody></tbody></table></div>
<div class="card"><h3>Longest prompts</h3><ul class="plist" id="lPrompts"></ul></div>
<div class="card"><h3>Most-edited files (by LoC)</h3><table id="tFiles"><thead><tr>
<th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table></div>
</div>
</div>

<div id="viewWeek" style="display:none">
<div class="card wide" style="margin-bottom:14px">
<div class="wknav">
<button id="wPrev">&larr; prev</button>
<h2 id="wTitle">…</h2>
<button id="wNext">next &rarr;</button>
<span class="sp"></span>
<button id="wCopy" class="act">&#128203; Copy as Markdown</button>
<button id="wAI" class="act">&#10024; AI summary</button>
</div>
<div class="grid" id="wKpis" style="margin-bottom:0"></div>
</div>
<div class="cards">
<div class="card wide"><h3>What you worked on</h3><div class="worklist" id="wWork"></div></div>
<div class="card"><h3>Exploration vs building</h3><canvas id="cSplit"></canvas></div>
<div class="card"><h3>This week, day by day</h3><canvas id="cWeekDays"></canvas></div>
<div class="card wide" id="wAIcard" style="display:none"><h3>AI-written summary</h3>
<div class="aiout" id="wAItext"></div>
<div class="note">Generated locally via <code>claude -p</code> — this is the only dashboard feature that consumes tokens, and only when you press the button.</div></div>
</div>
</div>

<div id="modal"><div class="mbox">
<button id="mClose">&times;</button>
<h2 id="mTitle"></h2>
<canvas id="cProj" style="max-height:220px"></canvas>
<h3>Sessions</h3><ul class="plist" id="mSess"></ul>
<h3>Top files</h3><table id="mFiles"><thead><tr><th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table>
</div></div>

<footer>Costs are estimates at public API pricing (incl. cache read/write rates) — if you're on a subscription plan the real marginal cost is $0. Edit the PRICING table in the script to tune.</footer>
<script>
const fmt=n=>n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':''+n;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtD=s=>{const p=s.split('-');return MON[+p[1]-1]+' '+(+p[2]);};
let days=30,charts={},D=null,wIdx=null,view='dash';

document.getElementById('range').onclick=e=>{if(e.target.dataset.d===undefined)return;
days=+e.target.dataset.d;[...e.target.parentNode.children].forEach(b=>b.classList.remove('on'));
e.target.classList.add('on');load();};
document.getElementById('tabDash').onclick=()=>setView('dash');
document.getElementById('tabWeek').onclick=()=>setView('week');
function setView(v){view=v;
document.getElementById('viewDash').style.display=v==='dash'?'':'none';
document.getElementById('viewWeek').style.display=v==='week'?'':'none';
document.getElementById('tabDash').classList.toggle('on',v==='dash');
document.getElementById('tabWeek').classList.toggle('on',v==='week');
if(D)render();}
function mk(id,cfg){if(charts[id])charts[id].destroy();charts[id]=new Chart(document.getElementById(id),cfg);}
Chart.defaults.color='#8b949e';Chart.defaults.borderColor='#30363d';

async function load(){
const r=await fetch('/api/stats?days='+days);D=await r.json();
document.getElementById('upd').textContent=D.generated;
document.getElementById('root').textContent=D.root||'~/.claude/projects';
var pw=document.getElementById('parsewarn');
if(D.totals.parse_errors>0){if(!pw){pw=document.createElement('div');pw.id='parsewarn';pw.style.cssText='color:#f0883e;font-size:12px;margin-top:4px';document.querySelector('.sub').after(pw);}pw.textContent='\u26a0 '+D.totals.parse_errors.toLocaleString()+' log entries could not be parsed - the log format may have changed; stats may be incomplete.';}else if(pw){pw.remove();}
if(wIdx===null&&D.weeks.length)wIdx=D.weeks.length-1;
if(wIdx!==null&&wIdx>=D.weeks.length)wIdx=D.weeks.length-1;
render();}

function render(){if(view==='dash')renderDash();else renderWeek();}

function renderDash(){
const d=D,t=d.totals;
const kpis=[['Input tokens',fmt(t.input),''],['Output tokens',fmt(t.output),''],
['Cache read',fmt(t.cache_read),t.cache_efficiency+'% cache hit'],
['Est. API cost','$'+t.cost,'$0 real cost on subscription'],
['Lines of code',fmt(t.loc),'written by Claude'],
['Prompts',fmt(t.prompts),t.avg_prompt_lines+' lines avg'],
['Sessions',t.sessions,t.avg_session_min+' min avg'],
['Interruptions',t.interruptions,t.tool_errors+' tool errors'],
['Your tokens',fmt(t.human_tokens),'typed in prompts (est.)'],
['Leverage',t.leverage+'x','Claude tokens per token you type']];
document.getElementById('kpis').innerHTML=kpis.map(k=>
`<div class="kpi"><div class="l">${k[0]}</div><div class="v">${k[1]}</div><div class="s">${k[2]}</div></div>`).join('');
const labels=d.days.map(x=>x.day.slice(5));
mk('cTokens',{type:'bar',data:{labels,datasets:[
{label:'Input',data:d.days.map(x=>x.input),backgroundColor:'#58a6ff'},
{label:'Output',data:d.days.map(x=>x.output),backgroundColor:'#3fb950'},
{label:'Cache read',data:d.days.map(x=>x.cache_read),backgroundColor:'#30363d'}]},
options:{scales:{x:{stacked:true},y:{stacked:true}},plugins:{legend:{position:'bottom'}}}});
mk('cBalance',{type:'line',data:{labels,datasets:[
{label:'You (typed)',data:d.days.map(x=>x.human_tokens),borderColor:'#d29922',backgroundColor:'rgba(210,153,34,.12)',fill:true,tension:.3},
{label:'Claude (output)',data:d.days.map(x=>x.output),borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,.12)',fill:true,tension:.3}]},
options:{scales:{y:{type:'logarithmic'}},plugins:{legend:{position:'bottom'}}}});
mk('cModels',{type:'doughnut',data:{labels:d.models.map(m=>m.name),
datasets:[{data:d.models.map(m=>m.output),backgroundColor:['#58a6ff','#3fb950','#d29922','#bc8cff','#f85149','#39d2c0']}]},
options:{plugins:{legend:{position:'bottom'}}}});
mk('cLoc',{type:'line',data:{labels,datasets:[{label:'LoC',data:d.days.map(x=>x.loc),
borderColor:'#bc8cff',backgroundColor:'rgba(188,140,255,.15)',fill:true,tension:.3}]},
options:{plugins:{legend:{display:false}}}});
mk('cRW',{type:'bar',data:{labels,datasets:[
{label:'Exploration (reads/searches)',data:d.days.map(x=>x.reads),backgroundColor:'#58a6ff'},
{label:'Building (file edits)',data:d.days.map(x=>x.writes),backgroundColor:'#3fb950'}]},
options:{scales:{x:{stacked:true},y:{stacked:true}},plugins:{legend:{position:'bottom'}}}});
mk('cFrict',{type:'bar',data:{labels,datasets:[
{label:'Interruptions',data:d.days.map(x=>x.interruptions),backgroundColor:'#f85149'},
{label:'Tool errors',data:d.days.map(x=>x.tool_errors),backgroundColor:'#d29922'}]},
options:{scales:{x:{stacked:true},y:{stacked:true}},plugins:{legend:{position:'bottom'}}}});
mk('cTools',{type:'bar',data:{labels:d.tools.map(x=>x[0]),datasets:[{data:d.tools.map(x=>x[1]),
backgroundColor:'#d29922'}]},options:{indexAxis:'y',plugins:{legend:{display:false}}}});
mk('cSlash',{type:'bar',data:{labels:d.slash.map(x=>x[0]),datasets:[{data:d.slash.map(x=>x[1]),
backgroundColor:'#39d2c0'}]},options:{indexAxis:'y',plugins:{legend:{display:false}}}});
const wd=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];const max=Math.max(1,...d.heatmap.flat());
let hm='<div></div>'+[...Array(24).keys()].map(h=>`<div style="text-align:center">${h}</div>`).join('');
d.heatmap.forEach((row,i)=>{hm+=`<div style="line-height:1.9">${wd[i]}</div>`+row.map((v,h)=>
`<div class="cell" title="${wd[i]} ${h}h: ${v}" style="background:${v?`rgba(63,185,80,${.15+.85*v/max})`:'#1c2430'}"></div>`).join('');});
document.getElementById('heat').innerHTML=hm;
document.querySelector('#tProj tbody').innerHTML=d.projects.map(p=>
`<tr><td title="${esc(p.name)}">${esc(p.name.split('/').pop()||p.name)}</td><td class="num">${fmt(p.input)}</td><td class="num">${fmt(p.output)}</td><td class="num">${fmt(p.loc)}</td><td class="num">${p.cost}</td></tr>`).join('');
document.querySelectorAll('#tProj tbody tr').forEach((tr,i)=>tr.onclick=()=>openProject(d.projects[i]));
document.querySelector('#tModels tbody').innerHTML=d.models.map(m=>
`<tr><td>${esc(m.name)}</td><td class="num">${m.msgs}</td><td class="num">${fmt(m.input)}</td><td class="num">${fmt(m.output)}</td><td class="num">${m.cost}</td></tr>`).join('');
document.querySelector('#tBranch tbody').innerHTML=d.branches.map(b=>
`<tr><td>${esc(b.name)}</td><td class="num">${b.msgs}</td><td class="num">${fmt(b.output)}</td><td class="num">${fmt(b.loc)}</td></tr>`).join('');
document.getElementById('lPrompts').innerHTML=d.longest_prompts.map(p=>
`<li><b>${p.lines} lines / ${p.words} words</b> · ${p.day}<br>${esc(p.preview)}…</li>`).join('');
document.querySelector('#tFiles tbody').innerHTML=d.top_files.map(f=>
`<tr><td>${esc(f[0])}</td><td class="num">${fmt(f[1])}</td></tr>`).join('');
}

// ---------------------------------------------------------------- weekly tab
const delta=(c,p)=>{if(p==null||!isFinite(c/p)||p===0)return'';
const pc=Math.round((c-p)/p*100);if(pc===0)return'<span style="color:var(--dim)">= last wk</span>';
const up=pc>0;return `<span style="color:${up?'var(--green)':'var(--red)'}">${up?'&#9650;':'&#9660;'} ${Math.abs(pc)}% vs last wk</span>`;};

function buildPct(w){const t=w.reads+w.writes;return t?Math.round(w.writes/t*100):0;}

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
`<div class="kpi"><div class="l">${k[0]}</div><div class="v">${k[1]}</div><div class="s">${k[2]||''}</div></div>`).join('');
const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
document.getElementById('wWork').innerHTML=Object.keys(g).length?Object.keys(g).map(p=>
`<h4>${esc(p)}</h4><ul>`+g[p].map(s=>
`<li>${esc(s.title)}<br><span class="meta">${fmtD(s.day)} · ${s.min} min · ${fmt(s.loc)} LoC · ${s.prompts} prompts</span></li>`).join('')+'</ul>').join('')
:'<span style="color:var(--dim)">No sessions logged this week.</span>';
mk('cSplit',{type:'doughnut',data:{labels:['Building (file edits)','Exploration (reads/searches)'],
datasets:[{data:[w.writes,w.reads],backgroundColor:['#3fb950','#58a6ff']}]},
options:{plugins:{legend:{position:'bottom'}}}});
mk('cWeekDays',{type:'bar',data:{labels:w.days.map(x=>fmtD(x.day)),datasets:[
{label:'Output tokens',data:w.days.map(x=>x.output),backgroundColor:'#58a6ff',yAxisID:'y'},
{label:'LoC',data:w.days.map(x=>x.loc),backgroundColor:'#bc8cff',yAxisID:'y1'}]},
options:{scales:{y:{position:'left'},y1:{position:'right',grid:{drawOnChartArea:false}}},
plugins:{legend:{position:'bottom'}}}});
}

document.getElementById('wPrev').onclick=()=>{if(wIdx>0){wIdx--;renderWeek();}};
document.getElementById('wNext').onclick=()=>{if(wIdx<D.weeks.length-1){wIdx++;renderWeek();}};

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
return md;}

document.getElementById('wCopy').onclick=async()=>{
if(!D||!D.weeks.length)return;
const btn=document.getElementById('wCopy');
try{await navigator.clipboard.writeText(weekMarkdown(D.weeks[wIdx]));btn.textContent='✔ Copied!';}
catch(e){const ta=document.createElement('textarea');ta.value=weekMarkdown(D.weeks[wIdx]);
document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();btn.textContent='✔ Copied!';}
setTimeout(()=>btn.innerHTML='&#128203; Copy as Markdown',2000);};

document.getElementById('wAI').onclick=async()=>{
if(!D||!D.weeks.length)return;
const btn=document.getElementById('wAI'),card=document.getElementById('wAIcard'),out=document.getElementById('wAItext');
btn.disabled=true;btn.textContent='Generating…';card.style.display='';
out.textContent='Running claude -p — this can take a minute…';
try{const r=await fetch('/api/ai_summary?week='+encodeURIComponent(D.weeks[wIdx].key));
const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
catch(e){out.textContent='Failed: '+e;}
btn.disabled=false;btn.innerHTML='&#10024; AI summary';};

// ---------------------------------------------------------- project deep-dive
function openProject(p){
document.getElementById('mTitle').textContent=p.name;
document.getElementById('modal').style.display='flex';
mk('cProj',{type:'bar',data:{labels:p.days.map(x=>x.day.slice(5)),datasets:[
{label:'Output tokens',data:p.days.map(x=>x.output),backgroundColor:'#58a6ff',yAxisID:'y'},
{label:'LoC',data:p.days.map(x=>x.loc),backgroundColor:'#bc8cff',yAxisID:'y1'}]},
options:{scales:{y:{position:'left'},y1:{position:'right',grid:{drawOnChartArea:false}}},
plugins:{legend:{position:'bottom'}}}});
const sess=D.sessions_list.filter(s=>s.project===p.name);
document.getElementById('mSess').innerHTML=sess.length?sess.map(s=>
`<li><b>${esc(s.title)}</b><br>${s.day} · ${s.min} min · ${fmt(s.loc)} LoC · ${s.prompts} prompts · ${fmt(s.output)} tokens out</li>`).join('')
:'<li>No session details available.</li>';
document.querySelector('#mFiles tbody').innerHTML=(p.files||[]).map(f=>
`<tr><td>${esc(f[0])}</td><td class="num">${fmt(f[1])}</td></tr>`).join('');}
document.getElementById('mClose').onclick=()=>document.getElementById('modal').style.display='none';
document.getElementById('modal').onclick=e=>{if(e.target.id==='modal')e.target.style.display='none';};

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
            try:
                days = int(q.get("days", ["30"])[0]) or None
            except ValueError:
                days = 30
            st = self.scanner.build_stats()
            data = stats_to_json(st, days)
            data["root"] = self.root
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
