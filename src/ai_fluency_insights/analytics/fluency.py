"""AI Fluency scores (Delegation, Description, Discernment, Diligence)."""

from .common import _rnd

# ---------------------------------------------------------------------------
# "My Adoption" -- one developer's adoption story. Four pillars (Habit, Range,
# Craft, Impact) are each scored 0-100 over a rolling window and blended into
# one Adoption Score. Pure functions over Stats; formulas in docs/METRICS.md.
# ---------------------------------------------------------------------------


def fluency_of(w) -> dict:
    """Python twin of the front end's fluencyOf(): the four 0-100 AI-fluency
    dimensions for one aggregate `w` (a week or an adoption window)."""
    sess = w["sessions"]
    code = [s for s in sess if s["loc"] > 0]
    prompts = w["prompts"]
    lev = w["output"] / w["human_tokens"] if w["human_tokens"] else 0
    code_share = len(code) / len(sess) if sess else 0
    delegation = _rnd(100 * (0.5 * code_share + 0.5 * min(1, lev / 50)))
    avg_w = w["prompt_words"] / prompts if prompts else 0
    pushback = w["corrections"] + w["interruptions"]
    one_shot = max(0, 1 - pushback / prompts) if prompts else 0
    if avg_w <= 0:
        sweet = 0
    elif avg_w < 8:
        sweet = avg_w / 8
    elif avg_w <= 150:
        sweet = 1
    else:
        sweet = max(0, 1 - (avg_w - 150) / 300)
    description = _rnd(100 * (0.65 * one_shot + 0.35 * sweet))
    engaged = min(1, pushback / max(1, prompts * 0.05))
    err_ctl = max(0, 1 - w["tool_errors"] / max(1, w["writes"] or 1))
    discernment = _rnd(100 * (0.5 * engaged + 0.5 * err_ctl))
    verified = (len([s for s in code if s["bash"] > 0]) / len(code)) if code else 0
    diligence = _rnd(100 * verified) if code else 0
    return {"delegation": delegation, "description": description,
            "discernment": discernment, "diligence": diligence,
            "lev": lev, "code_share": code_share, "avg_words": avg_w,
            "one_shot": one_shot, "engaged": engaged, "verified": verified,
            "ncode": len(code), "pushback": pushback}


FLUENCY_TIPS = {
    "delegation": "Try handing Claude whole tasks (build it and check it), not just questions.",
    "description": "Put the constraints, file paths and expected behavior in your first prompt.",
    "discernment": "Review diffs and say so when output misses. Pushing back is a good sign.",
    "diligence": "Ask Claude to run the tests or build after edits, so work gets verified.",
}
