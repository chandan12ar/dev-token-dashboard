"""My Adoption tab: four pillars, Adoption Score, levels and coach."""

import os
from collections import Counter
from datetime import date, datetime, timedelta

from ..config import ADOPTION, ADOPTION_LEVELS, LEVEL_GATES
from ..core.stats import Stats
from ..core.taxonomy import ADOPTION_FEATURES
from .common import _iso_monday, _rnd
from .flow import build_flow
from .fluency import FLUENCY_TIPS, fluency_of
from .quality import build_quality
from .sessions import classify_session, longest_focus
from .weekly import active_streak


def _adoption_sessions(st: Stats):
    """Per-session rows with a local start day, for window slicing."""
    rows = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
        except Exception:
            continue
        title = st.session_titles.get(sid) or ""
        rows.append({
            "day": a.strftime("%Y-%m-%d"), "min": max(0.0, (b - a).total_seconds()) / 60,
            "loc": s["loc"], "reads": s["reads"], "writes": s["writes"],
            "bash": s["bash"], "prompts": s["prompts"], "project": s["project"],
            "type": classify_session(title, s["reads"], s["writes"], s["loc"]),
        })
    return rows


def _window_agg(st: Stats, sess_rows, start: date, end: date) -> dict:
    """Everything one adoption window needs, summed over [start, end]."""
    s0, s1 = start.isoformat(), end.isoformat()
    days = sorted(d for d in st.days if s0 <= d <= s1 and st.days[d]["messages"] > 0)
    w = {"start": s0, "end": s1, "days": days, "sessions": [],
         "active_days": len(days)}
    for k in ("input", "output", "loc", "prompts", "reads", "writes", "interruptions",
              "tool_errors", "human_tokens", "prompt_words", "corrections",
              "late_msgs", "messages"):
        w[k] = sum(st.days[d][k] for d in days)
    w["sessions"] = [r for r in sess_rows if s0 <= r["day"] <= s1]
    w["active_min"] = sum(len(st.day_buckets.get(d, ())) * 10 for d in days)
    w["code_days"] = sum(1 for d in days if st.days[d]["loc"] > 0)
    return w


def _feature_status(st: Stats, w: dict) -> dict:
    """feature id -> days used inside the window (memory is project-based)."""
    used = {}
    for fid, *_ in ADOPTION_FEATURES:
        if fid == "memory":
            projs = {r["project"] for r in w["sessions"]}
            n = 0
            for p in projs:
                cwd = st.project_cwd.get(p)
                if cwd and (os.path.isfile(os.path.join(cwd, "CLAUDE.md")) or
                            os.path.isfile(os.path.join(cwd, "AGENTS.md"))):
                    n += 1
            used[fid] = n
        else:
            used[fid] = sum(1 for d in st.feature_days.get(fid, ())
                            if w["start"] <= d <= w["end"])
    return used


def _pillar_scores(st: Stats, w: dict) -> dict:
    cfg = ADOPTION
    span = (date.fromisoformat(w["end"]) - date.fromisoformat(w["start"])).days + 1
    # Habit: how often, and how evenly across the weeks in the window
    target = cfg["target_days"] * span / cfg["window_days"]
    buckets = max(1, span // 7)
    start = date.fromisoformat(w["start"])
    covered = 0
    for i in range(buckets):
        lo = (start + timedelta(days=7 * i)).isoformat()
        hi = (start + timedelta(days=7 * i + 6)).isoformat()
        if sum(1 for d in w["days"] if lo <= d <= hi) >= 2:
            covered += 1
    habit = _rnd(100 * (0.6 * min(1, w["active_days"] / target) + 0.4 * covered / buckets))
    # Range: how much of Claude Code you actually use, and how deep sessions go
    feats = _feature_status(st, w)
    n_used = sum(1 for v in feats.values() if v > 0)
    sess = w["sessions"]
    substantial = [r for r in sess
                   if r["reads"] + r["writes"] + r["bash"] >= cfg["substantial_actions"]]
    sub_share = len(substantial) / len(sess) if sess else 0
    range_ = _rnd(100 * (0.75 * min(1, n_used / cfg["features_full"]) + 0.25 * sub_share))
    # Craft: the four AI-fluency dimensions, averaged, blended with how smoothly work
    # flows and how well it is checked (the Flow and Quality tabs) when those have data
    fl = fluency_of(w)
    craft_base = _rnd((fl["delegation"] + fl["description"] + fl["discernment"] + fl["diligence"]) / 4)
    flow_d, qual_d = build_flow(st, w["start"], w["end"]), build_quality(st, w["start"], w["end"])
    parts = [(0.6, craft_base)]
    if flow_d["score"] is not None:
        parts.append((0.2, flow_d["score"]))
    if qual_d["score"] is not None:
        parts.append((0.2, qual_d["score"]))
    craft = _rnd(sum(a * b for a, b in parts) / sum(a for a, _ in parts))
    # Impact: code Claude wrote for you, and how many of your days it did so
    per_day = w["loc"] / w["active_days"] if w["active_days"] else 0
    code_day_share = w["code_days"] / w["active_days"] if w["active_days"] else 0
    impact = _rnd(100 * (0.5 * min(1, per_day / cfg["loc_target"]) + 0.5 * code_day_share))
    wt = cfg["weights"]
    overall = _rnd(wt["habit"] * habit + wt["range"] * range_ + wt["craft"] * craft
                   + wt["impact"] * impact)
    return {"habit": habit, "range": range_, "craft": craft, "impact": impact,
            "overall": overall, "feats": feats, "n_used": n_used, "covered": covered,
            "buckets": buckets, "sub_share": sub_share, "substantial": len(substantial),
            "per_day": per_day, "code_day_share": code_day_share, "fluency": fl,
            "craft_base": craft_base, "flow": flow_d["score"], "quality": qual_d["score"],
            "flow_d": flow_d, "quality_d": qual_d}


def adoption_level(score: int, pillars=None, history=None) -> dict:
    """Level for a score. Pro and Pro Max also need every pillar above a floor, and
    Pro Max a sustained run of high weekly scores. Without pillars/history only the
    score is checked (used for quick lookups)."""
    top = 0
    for i, (floor, _n, _d) in enumerate(ADOPTION_LEVELS):
        if score >= floor:
            top = i
    idx, blocker = top, None
    weakest = min(pillars, key=lambda k: pillars[k]) if pillars else None
    while idx > 0:
        name = ADOPTION_LEVELS[idx][1]
        gate = LEVEL_GATES.get(name)
        if not gate or pillars is None:
            break
        problem = None
        if min(pillars.values()) < gate["min_pillar"]:
            problem = "Lift %s (now %d) to %d to unlock %s." % (
                weakest.capitalize(), pillars[weakest], gate["min_pillar"], name)
        elif "sustain_weeks" in gate:
            recent = [h["score"] for h in (history or [])][-gate["sustain_weeks"]:]
            if len(recent) < gate["sustain_weeks"] or min(recent) < gate["sustain_score"]:
                problem = "Hold %d+ for %d weeks in a row to unlock %s." % (
                    gate["sustain_score"], gate["sustain_weeks"], name)
        if not problem:
            break
        if blocker is None:
            blocker = problem
        idx -= 1
    floor, name, desc = ADOPTION_LEVELS[idx]
    nxt = ADOPTION_LEVELS[idx + 1] if idx + 1 < len(ADOPTION_LEVELS) else None
    return {"index": idx, "name": name, "desc": desc, "floor": floor,
            "next_name": nxt[1] if nxt else None,
            "next_floor": nxt[0] if nxt else None,
            "to_next": max(0, nxt[0] - score) if nxt else 0,
            "blocker": blocker,
            "all": [{"floor": f, "name": n} for f, n, _ in ADOPTION_LEVELS]}


def _coach(st: Stats, cur: dict, w: dict, wellbeing: dict, ever: dict) -> list:
    """Up to three concrete next steps, ranked by how much they should help."""
    cands = []
    for fid, label, _desc, tip, weight in ADOPTION_FEATURES:
        if cur["feats"][fid] > 0:
            continue
        before = ever.get(fid, 0) > 0
        cands.append({
            "area": "range", "feature": fid, "severity": weight + (0 if before else 10),
            "title": "Try %s" % label.lower() if fid != "memory" else "Add a CLAUDE.md",
            "why": ("You've used %s before, but not in the last %d days."
                    % (label.lower(), ADOPTION["window_days"]) if before
                    else "You haven't tried %s yet." % label.lower()),
            "how": tip})
    if cur["habit"] < 70:
        cands.append({
            "area": "habit", "feature": None, "severity": (100 - cur["habit"]) * 0.7,
            "title": "Make it a steady habit",
            "why": "You were active on %d of the last %d days."
                   % (w["active_days"], ADOPTION["window_days"]),
            "how": "Pick one small task a day (a bug, a test, a commit message) and give it to Claude."})
    fl = cur["fluency"]
    dims = [("delegation", "Delegate bigger tasks"), ("description", "Write sharper first prompts"),
            ("discernment", "Review and steer more"), ("diligence", "Verify the work")]
    weakest = min(dims, key=lambda d: fl[d[0]])
    if fl[weakest[0]] < 70:
        cands.append({
            "area": "craft", "feature": None, "severity": (100 - fl[weakest[0]]) * 0.75,
            "title": weakest[1],
            "why": "%s is your lowest craft score (%d/100)." % (weakest[0].capitalize(), fl[weakest[0]]),
            "how": FLUENCY_TIPS[weakest[0]]})
    if cur["impact"] < 50 and w["active_days"] >= ADOPTION["min_active_days"]:
        cands.append({
            "area": "impact", "feature": None, "severity": (100 - cur["impact"]) * 0.45,
            "title": "Aim Claude at real changes",
            "why": "Claude wrote code on %d%% of your active days."
                   % _rnd(100 * cur["code_day_share"]),
            "how": "Start the day by handing Claude one concrete change (a fix, a refactor, a test) to ship."})
    if wellbeing["late_pct"] > 20:
        cands.append({
            "area": "wellbeing", "feature": None,
            "severity": min(90, 40 + wellbeing["late_pct"]),   # outranks "try a feature" tips
            "title": "Protect your evenings",
            "why": "%d%% of your activity happened between 11pm and 6am." % wellbeing["late_pct"],
            "how": "Claude will still be there tomorrow. Try a firm stop time for a week."})
    cands.sort(key=lambda c: -c["severity"])
    picked, feats = [], 0
    for c in cands:
        if c["feature"]:
            if feats >= 2:
                continue
            feats += 1
        picked.append(c)
        if len(picked) == 3:
            break
    return picked


def _highlights(cur, prev, w, pw, feature_rows, alldays, level) -> list:
    """Up to four plain-language lines about what moved since the previous window."""
    out = []
    names = {"habit": "Habit", "range": "Range", "craft": "Craft", "impact": "Impact"}
    if prev:
        moves = sorted(((cur[k] - prev[k], k) for k in names), key=lambda x: -abs(x[0]))
        for delta, k in moves[:2]:
            if abs(delta) < 5:
                continue
            extra = ""
            if k == "habit":
                extra = " (active on %d days, was %d)" % (w["active_days"], pw["active_days"])
            elif k == "range":
                extra = " (%d features in use, was %d)" % (cur["n_used"], prev["n_used"])
            elif k == "impact":
                extra = " (%s lines, was %s)" % (w["loc"], pw["loc"])
            out.append({"kind": "up" if delta > 0 else "down",
                        "text": "%s %s %d points%s" % (names[k], "rose" if delta > 0 else "fell", abs(delta), extra)})
        prev_level = adoption_level(prev["overall"])
        if prev_level["index"] != level["index"]:
            out.append({"kind": "level", "text": "Level change: %s \u2192 %s" % (prev_level["name"], level["name"])})
    for f in feature_rows:
        if f["first"] and alldays and f["first"] > alldays[0] and w["start"] <= f["first"] <= w["end"]:
            out.append({"kind": "new", "text": "First time using %s (%s)" % (f["label"].lower(), f["first"])})
    if not out:
        out.append({"kind": "flat", "text": "A steady stretch: no pillar moved by 5 points or more."})
    return out[:4]


def _funnel(st: Stats, alldays, cur, w) -> list:
    """Four stages of personal adoption, each with the evidence for it."""
    per_week = Counter(_iso_monday(d) for d in alldays)
    m, last = date.fromisoformat(_iso_monday(alldays[0])), date.fromisoformat(_iso_monday(alldays[-1]))
    run, rec_date = 0, None
    while m <= last:
        run = run + 1 if per_week.get(m.isoformat(), 0) >= 2 else 0
        if run >= 3 and rec_date is None:
            rec_date = (m + timedelta(days=6)).isoformat()
        m += timedelta(days=7)
    qd = cur["quality_d"]
    outcome = qd["edited_sessions"] >= 3 and (qd["verified_pct"] or 0) >= 50
    return [
        {"id": "started", "label": "Started", "achieved": True,
         "detail": "First active day: %s" % alldays[0]},
        {"id": "recurring", "label": "Recurring", "achieved": rec_date is not None,
         "detail": ("2+ active days a week for 3 weeks in a row (reached %s)" % rec_date) if rec_date
         else "Reach 2+ active days a week for 3 weeks in a row"},
        {"id": "integrated", "label": "Integrated", "achieved": cur["n_used"] >= 4,
         "detail": "%d features used in the last %d days (need 4)" % (cur["n_used"], ADOPTION["window_days"])},
        {"id": "outcome", "label": "Verified outcomes", "achieved": outcome,
         "detail": ("%d%% of edit sessions ended with a test, build or lint (need 50%%)" % qd["verified_pct"])
         if qd["edited_sessions"] >= 3 else "Needs 3+ sessions where Claude edited files"},
    ]


def build_adoption(st: Stats) -> dict:
    """Payload for the My Adoption tab. Always full history (like the weekly report)."""
    alldays = [d for d in sorted(st.days) if st.days[d]["messages"] > 0]
    cfg = ADOPTION
    out = {"ready": False, "min_days": cfg["min_active_days"], "window_days": cfg["window_days"],
           "active_days": 0, "targets": {k: cfg[k] for k in
                                         ("target_days", "features_full", "substantial_actions", "loc_target")},
           "weights": cfg["weights"]}
    if not alldays:
        return out
    try:
        end = date.fromisoformat(alldays[-1])
    except ValueError:
        return out
    span = cfg["window_days"]
    rows = _adoption_sessions(st)

    def window(end_d):
        return _window_agg(st, rows, end_d - timedelta(days=span - 1), end_d)

    w = window(end)
    out["active_days"] = w["active_days"]
    out["end"] = end.isoformat()
    out["start"] = w["start"]
    if w["active_days"] < cfg["min_active_days"]:
        return out
    cur = _pillar_scores(st, w)
    pw = window(end - timedelta(days=span))
    prev = _pillar_scores(st, pw) if pw["active_days"] >= cfg["min_active_days"] else None

    # --- history: the score as of each of the last 12 week-ends --------------
    history = []
    for i in range(11, -1, -1):
        e = end - timedelta(days=7 * i)
        hw = window(e)
        if hw["active_days"] >= cfg["min_active_days"]:
            history.append({"end": e.isoformat(), "score": _pillar_scores(st, hw)["overall"]})

    # --- wellbeing -----------------------------------------------------------
    wk_msgs = sum(st.days[d]["messages"] for d in w["days"]
                  if date.fromisoformat(d).weekday() >= 5)
    wellbeing = {
        "late_pct": _rnd(100 * w["late_msgs"] / w["messages"]) if w["messages"] else 0,
        "weekend_pct": _rnd(100 * wk_msgs / w["messages"]) if w["messages"] else 0,
        "avg_active_min": _rnd(w["active_min"] / w["active_days"]) if w["active_days"] else 0,
        "longest_focus_min": max((longest_focus(st.day_buckets.get(d, ())) for d in w["days"]), default=0),
    }

    # --- range: feature checklist -------------------------------------------
    ever = {}
    for fid, *_ in ADOPTION_FEATURES:
        ever[fid] = len(st.feature_days.get(fid, ()))
    ever["memory"] = 1 if cur["feats"]["memory"] > 0 else 0
    feature_rows = []
    for fid, label, desc, tip, _wt in ADOPTION_FEATURES:
        fd = st.feature_days.get(fid, ())
        feature_rows.append({
            "id": fid, "label": label, "desc": desc, "tip": tip,
            "recent": cur["feats"][fid], "ever": ever[fid] > 0,
            "first": min(fd) if fd else None})

    # --- craft ---------------------------------------------------------------
    fl, pfl = cur["fluency"], (prev["fluency"] if prev else None)
    dims = [
        ("delegation", "Delegation",
         "%d%% of sessions shipped code; %.1f× leverage" % (_rnd(100 * fl["code_share"]), fl["lev"])),
        ("description", "Description",
         "%d%% of prompts needed no correction; %d words on average"
         % (_rnd(100 * fl["one_shot"]), _rnd(fl["avg_words"]))),
        ("discernment", "Discernment",
         "%d corrections and interruptions; %d tool errors" % (fl["pushback"], w["tool_errors"])),
        ("diligence", "Diligence",
         "%d%% of %d code session%s ran commands afterwards"
         % (_rnd(100 * fl["verified"]), fl["ncode"], "" if fl["ncode"] == 1 else "s")),
    ]
    craft = [{"id": k, "label": lab, "score": fl[k], "prev": pfl[k] if pfl else None,
              "detail": det, "tip": FLUENCY_TIPS[k]} for k, lab, det in dims]
    fd, qd = cur["flow_d"], cur["quality_d"]
    if fd["score"] is not None:
        craft.append({"id": "flow", "label": "Flow", "score": fd["score"],
                      "prev": prev["flow"] if prev else None, "link": "flow",
                      "detail": "%s%% of turns were clean first tries; %d stuck loop%s"
                                % (fd["clean_pct"], fd["stuck"], "" if fd["stuck"] == 1 else "s"),
                      "tip": "Open the Flow tab to see what slows you down."})
    if qd["score"] is not None:
        craft.append({"id": "quality", "label": "Quality", "score": qd["score"],
                      "prev": prev["quality"] if prev else None, "link": "quality",
                      "detail": "%s%% of edit sessions verified; %s%% of edits revisit a file"
                                % (qd["verified_pct"], qd["reedit_pct"] if qd["reedit_pct"] is not None else 0),
                      "tip": "Open the Quality tab to see how your work gets checked."})

    # --- impact --------------------------------------------------------------
    mix = Counter(r["type"] for r in w["sessions"])
    impact = {
        "loc": w["loc"], "per_day": _rnd(cur["per_day"]),
        "code_day_pct": _rnd(100 * cur["code_day_share"]),
        "leverage": round(fl["lev"], 1), "sessions": len(w["sessions"]),
        "verified_pct": _rnd(100 * fl["verified"]), "task_mix": dict(mix),
        "prev_loc": pw["loc"] if prev else None,
    }

    # --- habit ---------------------------------------------------------------
    streak, best = active_streak(alldays)
    habit = {"active_days": w["active_days"], "weeks_covered": cur["covered"],
             "weeks": cur["buckets"], "streak": streak, "best_streak": best,
             "all_time_days": len(alldays),
             "prev_active_days": pw["active_days"] if prev else None}

    # --- weekly series (last 12 Mon-Sun weeks) + calendar --------------------
    monday = end - timedelta(days=end.weekday())
    weekly = []
    for i in range(11, -1, -1):
        m = monday - timedelta(days=7 * i)
        ww = _window_agg(st, rows, m, m + timedelta(days=6))
        weekly.append({"start": m.isoformat(), "active_days": ww["active_days"],
                       "loc": ww["loc"], "sessions": len(ww["sessions"])})
    cal_start = monday - timedelta(days=7 * 16)
    cells = []
    for i in range(17 * 7):
        d = cal_start + timedelta(days=i)
        if d > end:
            cells.append(-1)
        else:
            cells.append(len(st.day_buckets.get(d.isoformat(), ())) * 10)

    level = adoption_level(cur["overall"], {k: cur[k] for k in ("habit", "range", "craft", "impact")},
                           history)
    highlights = _highlights(cur, prev, w, pw, feature_rows, alldays, level)
    funnel = _funnel(st, alldays, cur, w)
    pillars = []
    for pid, label, blurb in (
            ("habit", "Habit", "How regularly you show up"),
            ("range", "Range", "How much of Claude Code you use"),
            ("craft", "Craft", "How well you work with Claude"),
            ("impact", "Impact", "What Claude gets done for you")):
        pillars.append({"id": pid, "label": label, "blurb": blurb, "score": cur[pid],
                        "prev": prev[pid] if prev else None})
    best_p = max(pillars, key=lambda p: p["score"])
    low_p = min(pillars, key=lambda p: p["score"])
    out.update({
        "ready": True, "score": cur["overall"],
        "prev_score": prev["overall"] if prev else None,
        "level": level, "pillars": pillars,
        "strength": best_p["id"], "growth": low_p["id"],
        "history": history, "habit": habit,
        "range": {"features": feature_rows, "used": cur["n_used"],
                  "total": len(ADOPTION_FEATURES), "substantial_pct": _rnd(100 * cur["sub_share"]),
                  "sessions": len(w["sessions"])},
        "craft": craft, "impact": impact, "wellbeing": wellbeing,
        "weekly": weekly, "calendar": {"start": cal_start.isoformat(), "cells": cells},
        "coach": _coach(st, cur, w, wellbeing, ever),
        "highlights": highlights, "funnel": funnel,
    })
    return out
