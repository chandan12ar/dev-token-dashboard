"""Optional `claude -p` features (the only ones that cost tokens; button-triggered)."""

from collections import Counter

# ---------------------------------------------------------------------------
# Optional AI weekly summary (runs `claude -p`; the ONE feature that costs
# tokens, triggered only by an explicit button press in the UI)
# ---------------------------------------------------------------------------

def _run_claude(prompt: str) -> dict:
    import shutil
    import subprocess
    # Resolve the absolute path once. Relying on the child process to re-resolve
    # "claude" on its own PATH (via `cmd /c claude`) is unreliable — it can pick
    # up a stale/broken shim and silently produce no output.
    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "error": "claude CLI not found on PATH."}
    # Call the resolved executable directly, passing the prompt as an argument
    # (avoids stdin-piping quirks). Only .cmd/.bat shims need the cmd wrapper.
    if exe.lower().endswith((".cmd", ".bat")):
        base = ["cmd", "/c", exe]
    else:
        base = [exe]
    try:
        r = subprocess.run(base + ["-p", prompt], capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude -p timed out after 5 minutes."}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    text = (r.stdout or "").strip()
    if text:
        return {"ok": True, "text": text}
    # No summary text — surface the real reason instead of a generic message.
    err = (r.stderr or "").strip()
    return {"ok": False, "error": "claude -p exited %d with no text (%s)"
            % (r.returncode, (err or "no stdout or stderr")[:400])}


def ai_week_summary(week: dict) -> dict:
    sess = week["sessions"]
    bullets = "\n".join(
        "- [%s] %s (%s, %s min, %s LoC)" % (s["project"], s["title"], s["day"], s["min"], s["loc"])
        for s in sess[:50]
    ) or "- (no sessions logged this week)"
    if len(sess) > 50:
        bullets += "\n- ...and %d more sessions" % (len(sess) - 50)
    hours = round(week["minutes"] / 60, 1)
    prompt = (
        "Write a short weekly work update for a developer's standup, covering "
        "%s to %s.\n\nCoding sessions this week:\n%s\n\n"
        "Stats: %s lines of code written, %s prompts, %s output tokens, "
        "~%s hours in sessions, tool mix %s file edits vs %s reads/searches.\n\n"
        "Format: one first-person paragraph of 3-5 sentences, then 3-6 short "
        "bullet points of key accomplishments. Plain markdown, no headers, no fluff."
        % (week["start"], week["end"], bullets, week["loc"], week["prompts"],
           week["output"], hours, week["writes"], week["reads"])
    )
    return _run_claude(prompt)


def ai_reflection(week: dict, question: str) -> dict:
    """Discuss the weekly reflection question with claude -p (button-only)."""
    types = Counter(s.get("type", "other") for s in week["sessions"])
    mix = ", ".join("%d %s" % (n, t) for t, n in types.most_common()) or "no sessions"
    prompt = (
        "You are helping a developer reflect on how they use AI coding tools. "
        "Their local usage dashboard asked them this reflection question:\n\n"
        "\"%s\"\n\n"
        "Context for the week %s to %s: %d sessions (%s), %s lines of code "
        "written via the AI, %s prompts (%s corrections, %s interruptions), "
        "%s file edits vs %s reads/searches, %s tool errors, ~%s active hours, "
        "%s messages during quiet hours (11pm-6am).\n\n"
        "Give a thoughtful, concise reflection (under 180 words): interpret what "
        "the data suggests, name one trade-off worth considering, and end with "
        "one concrete experiment to try next week. Plain text, no headers, "
        "address the developer as 'you'."
        % (question, week["start"], week["end"], len(week["sessions"]), mix,
           week["loc"], week["prompts"], week.get("corrections", 0),
           week["interruptions"], week["writes"], week["reads"],
           week["tool_errors"], round(week.get("active_min", 0) / 60, 1),
           week.get("late_msgs", 0))
    )
    return _run_claude(prompt)
