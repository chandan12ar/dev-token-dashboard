"""Quality tab: was the work checked, rework hotspots, safety signals."""

import os
from collections import Counter, defaultdict

from ..core.stats import Stats
from .common import _in, _iso_monday, _r1, _rate, _rnd, _sess_rows


def build_quality(st: Stats, d0=None, d1=None) -> dict:
    rows = _sess_rows(st, d0, d1)
    edited = [(sid, s, day) for sid, s, day in rows if s["last_write_seq"] >= 0]
    verified = [r for r in edited if r[1]["last_verify_seq"] > r[1]["last_write_seq"]]
    any_cmd = [r for r in edited if r[1]["last_cmd_seq"] > r[1]["last_write_seq"]]
    with_tests = [r for r in edited if r[1]["test_files"] > 0]

    # test / build / lint runs and how they went
    runs = [(day, sid, kind, ok) for day, sid, kind, ok in st.cmd_runs if _in(day, d0, d1)]
    groups = defaultdict(list)
    for _day, sid, kind, ok in runs:
        groups[(sid, kind)].append(ok)
    by_kind = {}
    for kind in ("test", "build", "lint"):
        ks = [(k, v) for k, v in groups.items() if k[1] == kind]
        n_runs = sum(len(v) for _k, v in ks)
        if not n_runs:
            continue
        failing = [v for _k, v in ks if False in v]
        recovered_g = [v for v in failing if True in v[v.index(False):]]
        by_kind[kind] = {
            "kind": kind, "runs": n_runs, "fails": sum(1 for _k, v in ks for ok in v if not ok),
            "groups": len(ks), "first_pass_pct": _rate(sum(1 for _k, v in ks if v[0]), len(ks)),
            "fail_groups": len(failing), "recovered_groups": len(recovered_g)}
    all_groups = list(groups.values())
    first_run_pass = _rate(sum(1 for v in all_groups if v[0]), len(all_groups)) if all_groups else None

    # rework proxy: the same file edited again and again
    edits = [(d, sid, path, proj) for d, sid, path, proj in st.edits if _in(d, d0, d1)]
    per_sess = Counter((sid, path) for _d, sid, path, _p in edits)
    reedits = sum(c - 1 for c in per_sess.values())
    reedit_pct = _rate(reedits, len(edits)) if edits else None
    files = defaultdict(lambda: {"n": 0, "days": set(), "sids": Counter(), "project": ""})
    for d, sid, path, proj in edits:
        f = files[path]
        f["n"] += 1
        f["days"].add(d)
        f["sids"][sid] += 1
        f["project"] = proj
    hot = []
    for path, f in files.items():
        if f["n"] >= 5 and (max(f["sids"].values()) >= 3 or len(f["days"]) >= 3):
            hot.append({"file": os.path.basename(path), "project": f["project"], "edits": f["n"],
                        "days": len(f["days"]), "sessions": len(f["sids"])})
    hot.sort(key=lambda x: -x["edits"])

    days = [d for d in st.days if _in(d, d0, d1)]
    n_writes = sum(st.days[d]["writes"] for d in days)
    n_prompts = sum(st.days[d]["prompts"] for d in days)
    errors = sum(st.days[d]["tool_errors"] for d in days)
    intr = sum(st.days[d]["interruptions"] for d in days)
    safety = Counter(c for d, c in st.safety if _in(d, d0, d1))
    perms = Counter((dec, src) for d, dec, src in st.perm_decisions if _in(d, d0, d1))

    weeks = defaultdict(lambda: [0, 0])
    for _sid, s, day in edited:
        w = weeks[_iso_monday(day)]
        w[0] += 1
        w[1] += 1 if s["last_verify_seq"] > s["last_write_seq"] else 0
    weekly = [{"start": k, "edited": v[0], "verified": v[1], "pct": _rate(v[1], v[0])}
              for k, v in sorted(weeks.items())]

    score = None
    if len(edited) >= 3:
        terms = [(0.40, len(verified) / len(edited)), (0.20, len(with_tests) / len(edited)),
                 (0.20, 1 - min(1.0, (reedit_pct or 0) / 100 / 0.5))]
        if first_run_pass is not None:
            terms.append((0.20, first_run_pass / 100))
        score = _rnd(100 * sum(w * v for w, v in terms) / sum(w for w, _ in terms))
    return {
        "score": score, "ready": len(edited) >= 3, "edited_sessions": len(edited),
        "verified_pct": _rate(len(verified), len(edited)), "verified": len(verified),
        "any_command_pct": _rate(len(any_cmd), len(edited)),
        "tests_alongside_pct": _rate(len(with_tests), len(edited)), "with_tests": len(with_tests),
        "runs": by_kind, "first_run_pass_pct": first_run_pass, "run_groups": len(all_groups),
        "reedit_pct": reedit_pct, "edits": len(edits), "hotspots": hot[:8],
        "errors_per_100_edits": _r1(100 * errors / n_writes) if n_writes else None,
        "tool_errors": errors, "interrupts_per_100_prompts": _r1(100 * intr / n_prompts) if n_prompts else None,
        "interruptions": intr, "safety": dict(safety),
        "permissions": [{"decision": k[0], "source": k[1], "n": v} for k, v in perms.most_common()],
        "weekly": weekly,
    }
