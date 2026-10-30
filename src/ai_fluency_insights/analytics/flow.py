"""Flow tab: turn time, clean first-pass turns, failure recovery."""

from collections import Counter, defaultdict

from ..core.stats import Stats
from .common import _in, _median, _p90, _r1, _rate, _rnd, _sess_minutes, _sess_rows


def build_flow(st: Stats, d0=None, d1=None) -> dict:
    turns = [t for t in st.turns if _in(t["day"], d0, d1)]
    done = [t for t in turns if t["secs"] is not None]
    secs = [t["secs"] for t in done]
    clean = [t for t in done if not t["err"] and not t["intr"] and not t["corrected"]]
    clean_pct = _rate(len(clean), len(turns))

    by_day = defaultdict(list)
    for t in turns:
        by_day[t["day"]].append(t)
    series = []
    for day in sorted(by_day):
        ts_ = by_day[day]
        dsecs = [t["secs"] for t in ts_ if t["secs"] is not None]
        series.append({"day": day, "turns": len(ts_), "median": _r1(_median(dsecs)),
                       "p90": _r1(_p90(dsecs)),
                       "clean_pct": _rate(sum(1 for t in ts_ if t["secs"] is not None and not t["err"]
                                              and not t["intr"] and not t["corrected"]), len(ts_))})

    # where tool time goes (sequential tool calls; excludes the model's own time)
    fam = defaultdict(lambda: {"calls": 0, "secs": [], "fails": 0})
    for day, _tool, family, sec, ok in st.tool_calls:
        if _in(day, d0, d1):
            f = fam[family]
            f["calls"] += 1
            f["secs"].append(sec)
            f["fails"] += 0 if ok else 1
    total_tool = sum(sum(f["secs"]) for f in fam.values())
    tools = sorted(({"family": k, "calls": v["calls"], "total_s": round(sum(v["secs"]), 1),
                     "share_pct": _rate(sum(v["secs"]), total_tool) if total_tool else 0,
                     "median_s": _r1(_median(v["secs"])), "p90_s": _r1(_p90(v["secs"])),
                     "max_s": _r1(max(v["secs"])), "fails": v["fails"]}
                    for k, v in fam.items()), key=lambda x: -x["total_s"])[:10]
    turn_total = sum(secs)
    tool_share = min(100, _rate(total_tool, turn_total)) if turn_total and total_tool else None

    # failure -> recovery episodes: the same tool failing, then succeeding later
    eps = [(day, tool, fails, steps, True) for day, tool, fails, steps in st.episodes if _in(day, d0, d1)]
    for sid, s, _day in _sess_rows(st, d0, d1):
        for tool, ep in s["open_err"].items():
            if _in(ep["day"], d0, d1):
                eps.append((ep["day"], tool, ep["fails"], None, False))
    n_eps = len(eps)
    recovered = [e for e in eps if e[4]]
    stuck = [e for e in eps if e[2] >= 3]
    recovery_pct = _rate(len(recovered), n_eps) if n_eps else None
    fail_tools = Counter(e[1] for e in eps).most_common(5)

    rows = _sess_rows(st, d0, d1)
    mins = [_sess_minutes(s) for _sid, s, _d in rows]
    first_edit = [s["first_edit_s"] for _sid, s, _d in rows if s["first_edit_s"] is not None]
    think = [ms / 1000 for day, ms in st.thinking if _in(day, d0, d1)]
    effort = Counter(e for day, e in st.efforts if _in(day, d0, d1))

    score = None
    if len(turns) >= 5:
        rec = (len(recovered) / n_eps) if n_eps else 1.0
        stuck_share = (len(stuck) / n_eps) if n_eps else 0.0
        score = _rnd(100 * (0.5 * (len(clean) / len(turns)) + 0.3 * rec + 0.2 * (1 - stuck_share)))
    return {
        "score": score, "ready": len(turns) >= 5, "turns": len(turns), "turns_done": len(done),
        "abandoned": len(turns) - len(done),
        "turn_median_s": _r1(_median(secs)), "turn_p90_s": _r1(_p90(secs)),
        "turn_max_s": _r1(max(secs)) if secs else None,
        "steps_median": _r1(_median([t["steps"] for t in done])),
        "clean_pct": clean_pct,
        "corrected": sum(1 for t in turns if t["corrected"]),
        "interrupted": sum(1 for t in turns if t["intr"]),
        "with_errors": sum(1 for t in turns if t["err"]),
        "series": series, "tools": tools, "tool_share_pct": tool_share,
        "episodes": n_eps, "recovered": len(recovered), "recovery_pct": recovery_pct,
        "unrecovered": n_eps - len(recovered), "stuck": len(stuck),
        "recover_steps_median": _r1(_median([e[3] for e in recovered if e[3] is not None])),
        "fail_tools": [{"tool": k, "episodes": v} for k, v in fail_tools],
        "sessions": len(rows), "session_median_min": _r1(_median(mins)),
        "session_p90_min": _r1(_p90(mins)),
        "first_edit_median_s": _r1(_median(first_edit)), "first_edit_n": len(first_edit),
        "thinking_median_s": _r1(_median(think)), "thinking_total_s": round(sum(think), 1) if think else 0,
        "thinking_share_pct": min(100, _rate(sum(think), turn_total)) if think and turn_total else None,
        "effort": [{"level": k, "n": v} for k, v in effort.most_common()],
    }
