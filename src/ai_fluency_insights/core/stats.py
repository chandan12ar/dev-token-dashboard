"""The in-memory aggregate that every analytics engine reads from."""

from collections import Counter, defaultdict


def _day_stat():
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": 0.0, "loc": 0, "prompts": 0, "prompt_lines": 0,
        "prompt_words": 0, "messages": 0, "human_tokens": 0,
        "reads": 0, "writes": 0, "interruptions": 0, "tool_errors": 0,
        "late_msgs": 0, "corrections": 0, "cache_savings": 0.0,
    }


def _proj_stat():
    return {
        "input": 0, "output": 0, "loc": 0, "prompts": 0,
        "sessions": set(), "cost": 0.0,
        "days": defaultdict(lambda: {"output": 0, "loc": 0}),
        "files": Counter(),
    }


class Stats:
    def __init__(self):
        self.days = defaultdict(_day_stat)
        self.models = defaultdict(lambda: {
            "msgs": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "cost": 0.0,
        })
        self.tools = Counter()
        self.slash = Counter()
        self.projects = defaultdict(_proj_stat)
        self.sessions = {}          # sid -> per-session record
        self.session_titles = {}    # sid -> AI-generated title from the logs
        self.branches = defaultdict(lambda: {"msgs": 0, "output": 0, "loc": 0})
        self.heatmap = defaultdict(int)   # (weekday, hour) -> messages
        self.day_buckets = defaultdict(set)  # day -> set of active 10-min buckets
        self.prompt_words = 0
        self.prompt_chars = 0
        self.longest_prompts = []   # list of (lines, preview, day)
        self.files_touched = Counter()
        self.seen_msg_ids = set()
        self.seen_tool_ids = set()
        self.errors = 0
        self.tool_errors = 0
        self.parse_errors = 0   # log entries we failed to parse (format drift)
        self.permission_modes = Counter()  # auto / default / acceptEdits snapshots
        self.subagents = Counter()         # Agent tool calls, by subagent_type
        self.project_cwd = {}              # project label -> real absolute path (first seen)
        self.last_day = None               # most recent dated entry seen (undated events piggyback on it)
        self.feature_days = defaultdict(set)  # feature id -> days it was used (see ADOPTION_FEATURES)
        # --- per-event records for the Flow / Quality / Value tabs. Every record
        # carries its local day, so any window can be sliced out of the full
        # history without re-parsing (see build_flow / build_quality / build_value).
        self.turns = []        # {day, sid, secs|None, steps, err, intr, corrected}
        self.tool_calls = []   # (day, tool, family, secs, ok)
        self.cmd_runs = []     # (day, sid, kind, ok)  kind in test/build/lint
        self.episodes = []     # (day, tool, fails, steps_to_recover|None)  closed failure episodes
        self.edits = []        # (day, sid, path, project)
        self.safety = []       # (day, category)
        self.perm_decisions = []   # (day, decision, source)
        self.thinking = []     # (day, ms)
        self.efforts = []      # (day, effort)
        self.pending_tools = {}    # tool_use_id -> info, until its result arrives
        # --- what the log contains (all-time, for the Data Health tab)
        self.hc = Counter()
        self.entry_types = Counter()
        self.attachment_types = Counter()
        self.versions = Counter()
        self.entrypoints = Counter()

    # -- merge helpers ------------------------------------------------------
    def touch_session(self, sid, ts, project):
        if not sid:
            return None
        s = self.sessions.setdefault(sid, {
            "start": ts, "end": ts, "msgs": 0, "project": project,
            "reads": 0, "writes": 0, "loc": 0, "prompts": 0, "output": 0,
            "bash": 0,
            "cost": 0.0, "first_call_cost": 0.0, "api_calls": 0, "cache_resets": 0,
            "seq": 0, "last_write_seq": -1, "last_verify_seq": -1, "last_cmd_seq": -1,
            "test_files": 0, "first_edit_s": None, "model_cost": Counter(),
            "turn": None, "last_turn": None, "open_err": {}, "start_dt": None,
        })
        if ts:
            if not s["start"] or ts < s["start"]:
                s["start"] = ts
            if not s["end"] or ts > s["end"]:
                s["end"] = ts
        s["msgs"] += 1
        return s
