"""JSON payloads behind the dashboard's /api endpoints, independent of HTTP."""

from datetime import datetime

from ..analytics.adoption import build_adoption
from ..analytics.advisor import build_advisor
from ..analytics.catalog import catalog_payload
from ..analytics.flow import build_flow
from ..analytics.health import build_health
from ..analytics.quality import build_quality
from ..analytics.summary import stats_to_json
from ..analytics.value import build_value
from ..analytics.weekly import active_streak, build_weeks
from ..config import GOALS
from ..core.stats import Stats
from ..integrations.claude_cli import ai_reflection, ai_week_summary


def stats_payload(scanner, root, watcher=None, days=30, frm="", to=""):
    """Everything the dashboard renders for one date range (rolling `days`, or `frm`..`to`)."""
    # Build an all-time Stats (powers the Weekly tab, streaks and deltas)
    # plus a range-scoped Stats so every card on the dashboard respects
    # the selected window — not just the headline KPIs.
    full = scanner.build_stats()
    alldays = sorted(full.days.keys())
    prev = []
    if frm or to:                       # custom date range
        cur = [d for d in alldays if (not frm or d >= frm) and (not to or d <= to)]
        ranged = scanner.build_stats(frm or None, to or None)
    elif days > 0:                       # rolling N-day window
        cur = alldays[-days:]
        prev = alldays[-2 * days:-days]  # the window just before it (for deltas)
        ranged = scanner.build_stats(cur[0]) if cur else Stats()
    else:                                # all time
        cur = alldays
        ranged = full
    data = stats_to_json(ranged, None)
    data["weeks"] = build_weeks(full)
    data["adoption"] = build_adoption(full)
    # Flow / Quality / Value follow the selected range, sliced from full history
    if frm or to:
        b0, b1 = frm or None, to or None
    elif days > 0 and cur:
        b0, b1 = cur[0], cur[-1]
    else:
        b0 = b1 = None
    data["flow"] = build_flow(full, b0, b1)
    data["quality"] = build_quality(full, b0, b1)
    data["value"] = build_value(full, b0, b1)
    if prev:
        keep = {"flow": ("turn_median_s", "clean_pct", "recovery_pct", "first_edit_median_s", "score"),
                "quality": ("verified_pct", "first_run_pass_pct", "tests_alongside_pct", "reedit_pct", "score"),
                "value": ("cost", "per_100_loc", "cost_per_verified", "cache_savings")}
        pf = {"flow": build_flow(full, prev[0], prev[-1]), "quality": build_quality(full, prev[0], prev[-1]),
              "value": build_value(full, prev[0], prev[-1])}
        data["prev_insight"] = {k: {m: pf[k][m] for m in ms} for k, ms in keep.items()}
    data["advisor"] = build_advisor(full, data["adoption"])
    data["health"] = build_health(full)
    data["catalog"] = catalog_payload()
    data["root"] = root

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
    data["notifications"] = list(watcher.recent_events[-20:]) if watcher else []
    data["plan_window"] = watcher.plan_window_snapshot() if watcher else None
    if alldays:
        last = alldays[-1]
        data["today"] = {"day": last, "loc": full.days[last]["loc"],
                         "tokens": full.days[last]["output"]}
        data["week_tokens"] = sum(full.days[d]["output"] for d in alldays[-7:])
    return data


def _find_week(scanner, wkey):
    return next((w for w in build_weeks(scanner.build_stats()) if w["key"] == wkey), None)


def ai_summary_payload(scanner, wkey):
    week = _find_week(scanner, wkey)
    if not week:
        return {"ok": False, "error": "unknown week %r" % wkey}
    return ai_week_summary(week)


def reflect_payload(scanner, wkey, question):
    week = _find_week(scanner, wkey)
    if not week:
        return {"ok": False, "error": "unknown week %r" % wkey}
    if not question:
        return {"ok": False, "error": "no question provided"}
    return ai_reflection(week, question)
