"""Weekly aggregation for the Weekly Report tab, plus streaks."""

from datetime import datetime

from ..core.stats import Stats
from .sessions import classify_session

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
