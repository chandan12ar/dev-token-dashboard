"""Value tab: cost per output, cache savings, model mix."""

from collections import Counter, defaultdict

from ..core.pricing import price_for
from ..core.stats import Stats
from .common import _in, _iso_monday, _median, _r1, _rate, _rnd, _sess_rows


def _is_top_tier(model: str) -> bool:
    return price_for(model)["in"] >= 10


def build_value(st: Stats, d0=None, d1=None) -> dict:
    days = sorted(d for d in st.days if _in(d, d0, d1))
    cost = sum(st.days[d]["cost"] for d in days)
    saved = sum(st.days[d]["cache_savings"] for d in days)
    loc = sum(st.days[d]["loc"] for d in days)
    rows = _sess_rows(st, d0, d1)
    sess_costs = [s["cost"] for _sid, s, _d in rows if s["cost"] > 0]
    edited = [r for r in rows if r[1]["last_write_seq"] >= 0]
    verified = [r for r in edited if r[1]["last_verify_seq"] > r[1]["last_write_seq"]]
    ver_cost = sum(r[1]["cost"] for r in verified)

    # cost spikes: days well above this window's own typical day
    day_costs = [st.days[d]["cost"] for d in days if st.days[d]["cost"] > 0]
    med = _median(day_costs) or 0
    spikes = []
    if len(day_costs) >= 5:
        for d in days:
            c = st.days[d]["cost"]
            if c >= max(2 * med, 0.5):
                top = max((r for r in rows if r[2] == d), key=lambda r: r[1]["cost"], default=None)
                spikes.append({"day": d, "cost": round(c, 2), "x_typical": round(c / med, 1) if med else None,
                               "session": (st.session_titles.get(top[0]) or "Untitled session") if top else None,
                               "session_cost": round(top[1]["cost"], 2) if top else None})
        spikes.sort(key=lambda x: -x["cost"])

    first = sum(s.get("first_call_cost", 0.0) for _sid, s, _d in rows)
    total_sess = sum(s.get("cost", 0.0) for _sid, s, _d in rows)
    nf = sum(max(0, s["api_calls"] - 1) for _sid, s, _d in rows)
    resets = sum(s["cache_resets"] for _sid, s, _d in rows if s["api_calls"] > 1)

    mcost = Counter()
    sess_by_model = Counter()
    top_tier_cost = top_tier_nocode = 0.0
    for _sid, s, _d in rows:
        for m, c in s["model_cost"].items():
            mcost[m] += c
        if s["model_cost"]:
            primary = s["model_cost"].most_common(1)[0][0]
            sess_by_model[primary] += 1
            if _is_top_tier(primary):
                top_tier_cost += s["cost"]
                if s["last_write_seq"] < 0:
                    top_tier_nocode += s["cost"]
    mtotal = sum(mcost.values())
    models = [{"model": m, "cost": round(c, 2), "share_pct": _rate(c, mtotal),
               "sessions": sess_by_model.get(m, 0)} for m, c in mcost.most_common(6)]

    weeks = defaultdict(lambda: [0.0, 0])
    for d in days:
        w = weeks[_iso_monday(d)]
        w[0] += st.days[d]["cost"]
        w[1] += st.days[d]["loc"]
    weekly = [{"start": k, "cost": round(v[0], 2), "loc": v[1],
               "per_100_loc": round(v[0] / v[1] * 100, 2) if v[1] else None}
              for k, v in sorted(weeks.items())]
    return {
        "ready": bool(days) and cost > 0, "cost": round(cost, 2), "loc": loc,
        "per_100_loc": round(cost / loc * 100, 2) if loc else None,
        "sessions": len(rows), "session_median_cost": round(_median(sess_costs), 2) if sess_costs else None,
        "verified_sessions": len(verified),
        "cost_per_verified": round(ver_cost / len(verified), 2) if verified else None,
        "cache_savings": round(saved, 2),
        "cache_saved_pct": _rnd(100 * saved / (cost + saved)) if (cost + saved) > 0 and saved > 0 else 0,
        "cold_start_pct": _r1(100 * first / total_sess) if total_sess else None,
        "cache_reset_pct": _r1(100 * resets / nf) if nf else None,
        "daily": [{"day": d, "cost": round(st.days[d]["cost"], 3),
                   "spike": any(sp["day"] == d for sp in spikes)} for d in days],
        "spikes": spikes[:5], "median_day_cost": round(med, 2) if med else None,
        "models": models,
        "top_tier_nocode_pct": _rate(top_tier_nocode, top_tier_cost) if top_tier_cost else None,
        "top_tier_cost": round(top_tier_cost, 2), "weekly": weekly,
    }
