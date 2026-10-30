"""Advisor tab: evidence-based next steps."""

from datetime import date, timedelta

from ..config import ADOPTION
from ..core.stats import Stats
from .flow import build_flow
from .quality import build_quality
from .value import build_value


def build_advisor(st: Stats, adoption: dict) -> dict:
    """Evidence-based next steps over the same 28-day window as My Adoption. Every
    recommendation quotes the numbers behind it; none of this uses AI or tokens."""
    out = {"ready": False, "recs": [], "more": [], "wins": [], "metrics": {}}
    end_day = adoption.get("end")
    if not end_day:
        return out
    d1 = end_day
    d0 = (date.fromisoformat(end_day) - timedelta(days=ADOPTION["window_days"] - 1)).isoformat()
    fl, q, v = build_flow(st, d0, d1), build_quality(st, d0, d1), build_value(st, d0, d1)
    out.update({"ready": True, "start": d0, "end": d1,
                "scores": {"flow": fl["score"], "quality": q["score"]}})
    recs, wins = [], []

    def add(rid, area, sev, title, evidence, action, conf, metric=None, better=None, label=None):
        rec = {"id": rid, "area": area, "severity": round(sev, 1), "title": title,
               "evidence": evidence, "action": action, "confidence": conf}
        if metric:
            rec["experiment"] = {"metric": metric, "better": better, "label": label}
        recs.append(rec)

    vp = q["verified_pct"]
    if q["edited_sessions"] >= 3 and vp is not None:
        if vp < 60:
            add("verify", "quality", (100 - vp) * 0.9, "Make Claude check its own work",
                "A test, build or lint ran after Claude's last edit in %d%% of edit sessions (%d of %d)."
                % (vp, q["verified"], q["edited_sessions"]),
                "End prompts with \"then run the tests\", or put your test command in CLAUDE.md so Claude runs it on its own.",
                "proxy", "verified_pct", "up", "Ask for a test run after every change, for your next 5 edit sessions")
        elif vp >= 80:
            wins.append("You check the work: %d%% of edit sessions ended with a test, build or lint." % vp)
    fp = q["first_run_pass_pct"]
    if fp is not None and q["run_groups"] >= 5:
        if fp < 60:
            add("firstrun", "quality", (100 - fp) * 0.8, "Your first test or build run often fails",
                "Only %d%% of first runs passed across %d session-and-check pairs." % (fp, q["run_groups"]),
                "Before new work, have Claude run the suite and fix what is red first; paste the failing output into the prompt.",
                "derived", "first_run_pass_pct", "up", "Start each session by running the tests, for your next 5 sessions")
        elif fp >= 85:
            wins.append("Your tests and builds pass first time: %d%% of first runs." % fp)
    cp = fl["clean_pct"]
    if fl["turns"] >= 8 and cp is not None:
        if cp < 60:
            add("clean", "flow", (100 - cp) * 0.8, "Sharper first prompts",
                "%d%% of turns were clean first tries: %d needed a correction, %d were interrupted and %d hit tool errors (of %d turns)."
                % (cp, fl["corrected"], fl["interrupted"], fl["with_errors"], fl["turns"]),
                "State the goal, the files involved and what \"done\" looks like in the first prompt. For bigger changes, start in plan mode.",
                "derived", "clean_pct", "up", "Write the goal, files and a done-check in your first prompt, for 5 sessions")
        elif cp >= 75:
            wins.append("Most of your turns land first time: %d%% clean." % cp)
    if fl["stuck"] >= 2:
        top = fl["fail_tools"][0]["tool"] if fl["fail_tools"] else "a tool"
        add("stuck", "flow", min(90, 45 + 10 * fl["stuck"]), "Break out of failure loops sooner",
            "%d times the same tool failed 3 or more times in a row (most often %s)." % (fl["stuck"], top),
            "After two failures, stop: restate the problem, change the approach, or switch to plan mode instead of letting Claude retry.",
            "derived", "stuck", "down", "After two failures, stop and change approach, for your next 5 sessions")
    if fl["episodes"] >= 5 and fl["recovery_pct"] is not None and fl["recovery_pct"] >= 85:
        wins.append("Failures get fixed: %d%% of failed tool calls were followed by a success." % fl["recovery_pct"])
    tools = [t for t in fl["tools"] if t["total_s"] >= 60]
    if tools and tools[0]["share_pct"] >= 35:
        t = tools[0]
        add("slowtool", "flow", 30 + t["share_pct"] * 0.4, "\u2018%s\u2019 dominates your tool time" % t["family"],
            "%s took %d%% of tool time: %ds over %d calls, slowest %ds." % (t["family"], t["share_pct"], t["total_s"], t["calls"], t["max_s"]),
            "Run it less often, scope it to the files you changed, or ask Claude to run it once at the end instead of after every edit.",
            "observed")
    sp90 = fl["session_p90_min"]
    if sp90 and fl["sessions"] >= 5 and (sp90 >= 180 or (fl["session_median_min"] or 0) >= 90):
        add("longsess", "flow", min(70, 30 + sp90 / 6), "Start a fresh session per task",
            "Median session is %d min and the longest 10%% run %d min."
            % (fl["session_median_min"] or 0, sp90),
            "Use /clear between unrelated tasks, and /compact when one long task has to continue.",
            "derived")
    cs = v["cold_start_pct"]
    if cs is not None and cs >= 45 and v["sessions"] >= 8:
        add("coldstart", "value", min(70, cs * 0.7), "Many short sessions are paying the cache set-up cost",
            "%.0f%% of session cost went on each session's first call, across %d sessions." % (cs, v["sessions"]),
            "Batch related small tasks into one session rather than opening a new one for each.",
            "estimated", "cold_start_pct", "down", "Do related small tasks in one session, for a week")
    cr = v["cache_reset_pct"]
    if cr is not None and cr >= 8:
        add("cachereset", "value", min(65, cr * 3), "The cache is being rebuilt mid-session",
            "%.0f%% of follow-up calls rebuilt the cache instead of reusing it." % cr,
            "Avoid long pauses in the middle of a task, and avoid switching tools or models partway through.",
            "estimated", "cache_reset_pct", "down", "Finish tasks without long pauses, for a week")
    rp = q["reedit_pct"]
    if rp is not None and q["edits"] >= 30:
        if rp >= 35:
            hot = ('; most reworked: "%s"' % q["hotspots"][0]["file"]) if q["hotspots"] else ""
            add("rework", "quality", min(75, rp), "Files are being edited again and again",
                "%d%% of edits were to a file already edited in that session%s." % (rp, hot),
                "Give Claude the full requirement and the check for \"done\" up front, so it makes one pass instead of many small ones.",
                "proxy", "reedit_pct", "down", "Describe the full requirement before Claude edits, for 5 sessions")
        elif rp <= 15:
            wins.append("You get things done in few passes: only %d%% of edits revisit a file." % rp)
    ta = q["tests_alongside_pct"]
    if q["edited_sessions"] >= 5 and ta is not None and ta < 40:
        add("tests", "quality", (100 - ta) * 0.5, "Ask for tests alongside code",
            "Claude touched a test file in only %d%% of edit sessions." % ta,
            "End prompts with \"and add or update the tests for this change\".",
            "proxy", "tests_alongside_pct", "up", "Ask for tests with every change, for 5 sessions")
    risky = {k: n for k, n in q["safety"].items() if k != "secret_file_touched"}
    if risky:
        n = sum(risky.values())
        add("risky", "quality", min(85, 40 + 10 * n), "Risky commands ran",
            "%d risky command%s: %s." % (n, "" if n == 1 else "s", ", ".join("%s x%d" % (k.replace("_", " "), c) for k, c in risky.items())),
            "Add deny rules in your Claude Code permission settings for the ones you never want to run unattended.",
            "proxy")
    if q["safety"].get("secret_file_touched"):
        n = q["safety"]["secret_file_touched"]
        add("secrets", "quality", min(90, 55 + 10 * n), "Claude touched secret-like files",
            "%d read or edit%s of files like .env, keys or credentials." % (n, "" if n == 1 else "s"),
            "Deny reads of .env and key files in your permission settings, or keep secrets outside the project folder.",
            "proxy")
    if v["spikes"]:
        s = v["spikes"][0]
        add("spike", "value", min(70, 35 + (s["x_typical"] or 0) * 5), "Cost spiked on %s" % s["day"],
            "$%.2f was %.1fx a typical day; biggest session: \"%s\" ($%.2f)." % (s["cost"], s["x_typical"] or 0, s["session"], s["session_cost"] or 0),
            "Check that session for a runaway loop or an oversized context, and split big tasks.",
            "estimated")
    tn = v["top_tier_nocode_pct"]
    if tn is not None and tn >= 50 and v["top_tier_cost"] >= 5:
        add("model", "value", 35, "Top-tier model on sessions with no edits",
            "%d%% of top-tier-model spend ($%.2f) went to sessions where Claude changed nothing." % (tn, v["top_tier_cost"]),
            "For questions and exploration try a lighter model, and keep the top tier for building.",
            "proxy")
    for c in adoption.get("coach", []):
        add("adopt_" + (c.get("feature") or c["area"]), c["area"] if c["area"] != "wellbeing" else "habit",
            min(80, c.get("severity", 40)), c["title"], c["why"], c["how"], "derived")
    if (adoption.get("habit") or {}).get("streak", 0) >= 5:
        wins.append("You're on a %d-day streak." % adoption["habit"]["streak"])
    if v["cache_saved_pct"] >= 20 and v["cache_savings"] >= 1:
        wins.append("Caching saved you about %d%% (about $%.2f at list prices)." % (v["cache_saved_pct"], v["cache_savings"]))
    if q["edits"] >= 20 and not q["safety"]:
        wins.append("No risky commands or secret-file touches in this window.")

    recs.sort(key=lambda r: -r["severity"])
    for i, r in enumerate(recs):
        r["rank"] = i + 1
    out["recs"], out["more"] = recs[:5], recs[5:12]
    out["wins"] = wins[:4]
    out["metrics"] = {
        "verified_pct": vp, "first_run_pass_pct": fp, "clean_pct": cp, "stuck": fl["stuck"],
        "reedit_pct": rp, "tests_alongside_pct": ta, "recovery_pct": fl["recovery_pct"],
        "cold_start_pct": cs, "cache_reset_pct": cr, "per_100_loc": v["per_100_loc"]}
    return out
