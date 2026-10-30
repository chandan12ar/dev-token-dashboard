"""Data Health tab: what this machine's logs can and cannot measure."""

from collections import Counter

from ..core.stats import Stats


def _field_status(n, d):
    if not d:
        return "missing"
    r = n / d
    return "ok" if r >= 0.9 else ("partial" if n else "missing")


def build_health(st: Stats) -> dict:
    """What this machine's logs actually contain, so a metric can say 'not in your logs'
    instead of showing a misleading zero."""
    hc = st.hc
    perms = Counter(dec for _d, dec, _s in st.perm_decisions)
    caps = [
        ("tokens", "Tokens and cost", "tokens",
         _field_status(hc["assistant_msgs"], hc["assistant_msgs"]),
         "%d responses with token usage (%d duplicate log lines dropped)" % (hc["assistant_msgs"], hc["duplicate_lines"])),
        ("turns", "Turn timing", "flow",
         "ok" if len(st.turns) else "missing",
         "%d turns timed from your prompt to Claude finishing" % len(st.turns)),
        ("tooltime", "Tool timing", "flow",
         _field_status(hc["tool_results_matched"], hc["tool_results"]),
         "%d of %d tool results matched to their call" % (hc["tool_results_matched"], hc["tool_results"])),
        ("cmdout", "Command outcomes", "quality",
         "ok" if hc["tool_results"] else "missing",
         "Pass/fail comes from the error flag; exit-code text seen on %d failed commands" % hc["exit_code_text"]),
        ("perms", "Permission decisions", "quality",
         "ok" if hc["permission_decisions"] else "missing",
         ("Seen: " + ", ".join("%s %d" % (k, v) for k, v in perms.most_common())) if perms
         else "None in these logs, so accept/reject rates are not shown"),
        ("thinking", "Thinking time", "flow",
         "ok" if hc["thinking"] else "missing", "%d responses report thinking time" % hc["thinking"]),
        ("effort", "Effort level", "flow",
         "ok" if hc["effort"] else "missing", "%d responses report an effort level" % hc["effort"]),
        ("subagents", "Subagent activity", "adoption",
         "ok" if (hc["sidechain_entries"] or sum(st.subagents.values())) else "missing",
         "%d subagent call%s, %d subagent log lines" % (sum(st.subagents.values()),
                                                       "" if sum(st.subagents.values()) == 1 else "s", hc["sidechain_entries"])
         if (hc["sidechain_entries"] or sum(st.subagents.values())) else "No subagent work seen yet"),
        ("hooks", "Hooks", "adoption",
         "ok" if hc["stop_hook_summary"] else "missing",
         "%d hook runs recorded" % hc["stop_hook_summary"] if hc["stop_hook_summary"] else "No hook runs seen"),
        ("time", "Timestamps", "all",
         _field_status(hc["entries_dated"], hc["entries"]),
         "%d of %d entries carry a timestamp" % (hc["entries_dated"], hc["entries"])),
    ]
    titled = sum(1 for sid in st.sessions if sid in st.session_titles)
    caps.append(("titles", "Session titles", "weekly",
                 _field_status(titled, len(st.sessions)),
                 "%d of %d sessions have an AI title" % (titled, len(st.sessions))))
    return {
        "capabilities": [{"id": i, "label": l, "tab": t, "status": s, "detail": d} for i, l, t, s, d in caps],
        "entry_types": [{"type": str(k), "n": v} for k, v in st.entry_types.most_common()],
        "versions": [{"version": k, "n": v} for k, v in st.versions.most_common(6)],
        "entrypoints": [{"name": k, "n": v} for k, v in st.entrypoints.most_common(6)],
        "parse_errors": st.parse_errors, "duplicate_lines": hc["duplicate_lines"],
        "sessions": len(st.sessions),
        "limits": [
            "Edits Claude makes through shell commands (sed, python scripts) are not counted as edits or lines of code.",
            "Turn and tool times include any time spent waiting for you to approve an action.",
            "The logs do not show which of Claude's output you kept, so there is no true acceptance rate unless permission decisions appear.",
            "A session is not a task: a long session can hold several tasks, and one task can span sessions.",
            "Cost is an estimate at public list prices; on a subscription plan your real marginal cost is $0.",
        ],
    }


# (id, tab, name, confidence, how it is computed, main limitation)
