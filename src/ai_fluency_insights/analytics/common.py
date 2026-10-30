"""Shared helpers for the Flow / Quality / Value / Adoption engines."""

import math
import statistics
from datetime import date, datetime, timedelta

from ..core.stats import Stats

# ---------------------------------------------------------------------------
# Flow / Quality / Value engines. Each takes the full-history Stats plus an
# optional [d0, d1] day window (ISO strings) and returns plain dicts for the UI.
# Nothing here reads outside the logs; every metric is tagged in METRIC_CATALOG
# as Observed / Derived / Proxy / Estimated so it is never overstated.
# ---------------------------------------------------------------------------

def _in(day, d0, d1) -> bool:
    return bool(day) and (d0 is None or day >= d0) and (d1 is None or day <= d1)


def _median(xs):
    return statistics.median(xs) if xs else None


def _p90(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(0.9 * len(xs)) - 1))]


def _rate(n, d):
    return _rnd(100 * n / d) if d else None


def _r1(x):
    return None if x is None else round(x, 1)


def _sess_rows(st: Stats, d0=None, d1=None):
    """(sid, session, local start day) for sessions starting inside the window."""
    rows = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
        except Exception:
            continue
        day = a.strftime("%Y-%m-%d")
        if _in(day, d0, d1):
            rows.append((sid, s, day))
    return rows


def _sess_minutes(s):
    try:
        a = datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(s["end"].replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds()) / 60
    except Exception:
        return 0.0


def _iso_monday(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def _rnd(x) -> int:
    """Round half up (matches the front end's Math.round, unlike banker's round)."""
    return int(x + 0.5)
