"""Task-type classification and wellness helpers."""


# ---------------------------------------------------------------------------
# Task-type classification (heuristic: AI session title first, tool mix as
# fallback) and wellness helpers
# ---------------------------------------------------------------------------

TASK_TYPES = ["feature", "bugfix", "refactor", "docs", "explore", "other"]


def classify_session(title, reads=0, writes=0, loc=0):
    t = " %s " % (title or "").lower()

    def has(*words):
        return any(w in t for w in words)

    if has("fix", " bug", "error", "issue", "crash", "debug", "broken",
           "fail", "troubleshoot", "repair", "not work"):
        return "bugfix"
    if has("refactor", "clean", "rename", "simplif", "reorganiz",
           "restructur", "tidy", "polish", "dedup"):
        return "refactor"
    if has("readme", "document", " docs", " doc ", "changelog",
           "write-up", "writeup", "comment"):
        return "docs"
    if has("add ", "implement", "build", "creat", " new ", "feature",
           "support", "integrat", "set up", "setup", "install", "launch",
           "generat", "redesign", "improve", "update", "upgrade"):
        return "feature"
    if has("explor", "investigat", "understand", "analy", "review",
           "explain", "research", "compare", "look at", "check ",
           "question", "how ", "why ", "what "):
        return "explore"
    # no title signal — fall back to what the session actually did
    if loc > 0 or writes > reads:
        return "feature"
    if reads:
        return "explore"
    return "other"


def longest_focus(buckets) -> int:
    """Longest run of consecutive 10-min activity buckets within a day, in minutes."""
    if not buckets:
        return 0
    b = sorted(buckets)
    best = run = 1
    for i in range(1, len(b)):
        run = run + 1 if b[i] == b[i - 1] + 1 else 1
        best = max(best, run)
    return best * 10
