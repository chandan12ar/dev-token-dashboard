"""The dashboard-wide stats payload for a date range."""

import os
from datetime import datetime

from ..config import QUIET_END, QUIET_START
from ..core.stats import Stats
from .sessions import classify_session, longest_focus
from .weekly import build_weeks


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
    # cold-start tax: what share of session cost is just the first API call
    # of each session (re-priming the cache) -- see docs/METRICS.md.
    total_session_cost = 0.0
    cold_start_cost = 0.0
    # cache-health: mid-session calls (not the session's first) where new
    # cache-write material outweighs reused cache-read material, a sign the
    # prefix cache was invalidated (idle >TTL, tool-list/model change, ...).
    non_first_calls = 0
    cache_reset_calls = 0
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
        total_session_cost += s.get("cost", 0.0)
        cold_start_cost += s.get("first_call_cost", 0.0)
        calls = s.get("api_calls", 0)
        if calls > 1:
            non_first_calls += calls - 1
            cache_reset_calls += s.get("cache_resets", 0)
    sessions_count = len(sess_durs)
    avg_session_min = (sum(sess_durs) / sessions_count / 60) if sessions_count else 0
    cold_start_pct = (cold_start_cost / total_session_cost * 100) if total_session_cost else 0
    cache_reset_pct = (cache_reset_calls / non_first_calls * 100) if non_first_calls else 0

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
            "cold_start_pct": round(cold_start_pct, 1),
            "cache_reset_pct": round(cache_reset_pct, 1),
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
