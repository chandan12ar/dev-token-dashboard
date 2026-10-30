"""Turns Claude Code JSONL log entries into Stats."""

import os
import re
from datetime import datetime

from ..config import QUIET_END, QUIET_START
from .pricing import price_for
from .stats import Stats
from .taxonomy import (
    CMD_TOOLS,
    CODE_TOOLS_EDIT,
    CODE_TOOLS_WRITE,
    EXIT_RE,
    READ_TOOLS,
    SECRET_PATH_RE,
    TEST_FILE_RE,
    TOOL_FEATURES,
    WRITE_TOOLS,
    classify_cmd,
    cmd_family,
    risky_categories,
)


def _entry_time(entry):
    try:
        ts = datetime.fromisoformat((entry.get("timestamp") or "").replace("Z", "+00:00")).astimezone()
        return ts, ts.strftime("%Y-%m-%d")
    except Exception:
        return None, ""


def _tool_result_text(block) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def _parse_aux_entry(entry, etype, st, min_day, max_day):
    """System and attachment lines: hooks and (for the health panel) what the log contains."""
    ts, day = _entry_time(entry)
    if not day:
        return
    if (min_day is not None and day < min_day) or (max_day is not None and day > max_day):
        return
    if etype == "system" and entry.get("subtype") == "stop_hook_summary":
        st.hc["stop_hook_summary"] += 1
        st.feature_days["hooks"].add(day)
    elif etype == "attachment":
        kind = (entry.get("attachment") or {}).get("type")
        if kind:
            st.attachment_types[kind] += 1


CMD_RE = re.compile(r"<command-name>\s*(/?[\w:_-]+)\s*</command-name>")

# prompts that read as corrections of Claude's previous output (heuristic,
# matched at the start of the prompt) — a signal for the fluency report
CORRECT_RE = re.compile(
    r"^(no+[,.! ]|no+$|not (that|what|like)|wrong|nope|incorrect|actually[, ]"
    r"|that'?s (not|wrong)|stop[,.! ]|undo |revert |don'?t |you (missed|forgot|broke))",
    re.IGNORECASE)


def count_lines(text) -> int:
    if not isinstance(text, str) or not text:
        return 0
    return text.count("\n") + 1


def _begin_turn(st, sess, sid, day, ts, is_correction):
    """A real prompt starts a turn; a prompt that follows an unfinished turn closes it as abandoned."""
    prev = sess["turn"]
    if prev is not None:
        prev["secs"] = None
        prev.pop("start", None)
        st.turns.append(prev)
        sess["last_turn"] = prev
    last = sess["last_turn"]
    if is_correction and last is not None:
        last["corrected"] = True
    sess["turn"] = {"day": day, "sid": sid, "start": ts, "secs": None, "steps": 0,
                    "err": False, "intr": False, "corrected": False}


def _end_turn(st, sess, ts):
    turn = sess["turn"]
    if turn is None or ts is None or turn.get("start") is None:
        return
    turn["secs"] = max(0.0, (ts - turn.pop("start")).total_seconds())
    st.turns.append(turn)
    sess["last_turn"] = turn
    sess["turn"] = None


def _handle_tool_results(content, st, sess, day, ts, side):
    """Every tool_result block: error counts (as before), tool timing, test/build/lint
    outcomes, and failure -> recovery episodes."""
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        is_err = bool(b.get("is_error"))
        if is_err:
            st.tool_errors += 1
            if day:
                st.days[day]["tool_errors"] += 1
        st.hc["tool_results"] += 1
        info = st.pending_tools.pop(b.get("tool_use_id"), None)
        if not info:
            continue
        st.hc["tool_results_matched"] += 1
        name = info["name"]
        if day and ts is not None and info["ts"] is not None:
            st.tool_calls.append((day, name, info["family"],
                                  max(0.0, (ts - info["ts"]).total_seconds()), not is_err))
        if is_err and name in CMD_TOOLS and EXIT_RE.search(_tool_result_text(b)):
            st.hc["exit_code_text"] += 1
        if day and info["kind"] in ("test", "build", "lint"):
            st.cmd_runs.append((day, info["sid"], info["kind"], not is_err))
        if sess is None or info["side"]:
            continue
        if sess["turn"] is not None and is_err:
            sess["turn"]["err"] = True
        oe = sess["open_err"]
        for ep in oe.values():
            ep["steps"] += 1
        if is_err:
            ep = oe.get(name)
            if ep is None:
                oe[name] = {"fails": 1, "steps": 0, "day": day}
            else:
                ep["fails"] += 1
        else:
            ep = oe.pop(name, None)
            if ep:
                st.episodes.append((ep["day"], name, ep["fails"], ep["steps"]))


def parse_entry(entry: dict, project: str, st: Stats, min_day=None, max_day=None):
    etype = entry.get("type")
    st.entry_types[etype] += 1
    if entry.get("version"):
        st.versions[entry["version"]] += 1
    if entry.get("entrypoint"):
        st.entrypoints[entry["entrypoint"]] += 1

    # AI-generated session title lines (written by Claude Code itself)
    if etype == "ai-title":
        sid, title = entry.get("sessionId"), entry.get("aiTitle")
        if sid and title:
            st.session_titles[sid] = title
        return

    # permission-mode snapshots ("auto" / "default" / "acceptEdits") carry no
    # timestamp of their own — they piggyback on the most recently seen dated
    # entry so they still respect the selected date range.
    if etype == "permission-mode":
        day = st.last_day
        if min_day is not None and (not day or day < min_day):
            return
        if max_day is not None and (not day or day > max_day):
            return
        st.permission_modes[entry.get("permissionMode") or "unknown"] += 1
        if entry.get("permissionMode") == "plan" and day:
            st.feature_days["plan"].add(day)
        return

    if etype in ("system", "attachment"):
        _parse_aux_entry(entry, etype, st, min_day, max_day)
        return

    if etype not in ("user", "assistant"):
        return
    if entry.get("isMeta"):
        return

    ts_raw = entry.get("timestamp") or ""
    day, hour, weekday, ts = "", None, None, None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone()
        day = ts.strftime("%Y-%m-%d")
        hour, weekday = ts.hour, ts.weekday()
    except Exception:
        pass
    if day:
        st.last_day = day

    cwd = entry.get("cwd")
    if cwd and project not in st.project_cwd:
        st.project_cwd[project] = cwd

    # date-window filter (used to build a range-scoped Stats). When no window is
    # set (all-time) undated entries are kept exactly as before.
    if min_day is not None and (not day or day < min_day):
        return
    if max_day is not None and (not day or day > max_day):
        return

    sid = entry.get("sessionId")
    branch = entry.get("gitBranch") or "(none)"
    sess = st.touch_session(sid, ts_raw, project)
    side = bool(entry.get("isSidechain"))      # subagent work, kept out of turn/verification tracking
    st.hc["entries"] += 1
    if day:
        st.hc["entries_dated"] += 1
    if side:
        st.hc["sidechain_entries"] += 1
        if day:
            st.feature_days["subagents"].add(day)
    if sess is not None and ts is not None and (sess["start_dt"] is None or ts < sess["start_dt"]):
        sess["start_dt"] = ts
    pdec = entry.get("permissionDecision")
    if isinstance(pdec, dict) and day:
        st.hc["permission_decisions"] += 1
        st.perm_decisions.append((day, str(pdec.get("decision")), str(pdec.get("source"))))
    if day:
        st.days[day]["messages"] += 1
    if hour is not None:
        st.heatmap[(weekday, hour)] += 1
        if day:
            # 10-minute activity buckets — the basis for "active time"
            st.day_buckets[day].add(hour * 6 + ts.minute // 10)
            if hour >= QUIET_START or hour < QUIET_END:
                st.days[day]["late_msgs"] += 1

    msg = entry.get("message") or {}
    content = msg.get("content")

    # ------------------------------------------------------------------ user
    if etype == "user":
        # failed tool results ride back on user-type entries
        if isinstance(content, list):
            _handle_tool_results(content, st, sess, day, ts, side)
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                text = "\n".join(texts)
        if not text:
            return
        # slash commands
        cmd = CMD_RE.search(text)
        if cmd:
            st.slash[cmd.group(1)] += 1
            if day:
                st.feature_days["slash"].add(day)
            return  # command invocations aren't hand-typed prompts
        if text.startswith("<local-command") or text.startswith("<command-"):
            return
        if "[Request interrupted" in text:
            st.errors += 1
            if day:
                st.days[day]["interruptions"] += 1
            if sess is not None and not side and sess["turn"] is not None:
                sess["turn"]["intr"] = True
            return
        lines = count_lines(text)
        words = len(text.split())
        if day and CORRECT_RE.match(text.strip()):
            st.days[day]["corrections"] += 1
        if day:
            st.days[day]["prompts"] += 1
            st.days[day]["prompt_lines"] += lines
            st.days[day]["prompt_words"] += words
            # ~4 chars per token: estimate of tokens the human typed
            st.days[day]["human_tokens"] += max(1, len(text) // 4)
        if sess is not None and not side:
            _begin_turn(st, sess, sid, day, ts, bool(CORRECT_RE.match(text.strip())))
        st.prompt_words += words
        st.prompt_chars += len(text)
        st.projects[project]["prompts"] += 1
        if sess:
            sess["prompts"] += 1
        preview = text.strip().replace("\n", " ")[:120]
        st.longest_prompts.append((lines, words, preview, day))
        st.longest_prompts.sort(key=lambda x: -x[0])
        del st.longest_prompts[40:]   # keep a pool so range-filtering still yields 8
        return

    # -------------------------------------------------------------- assistant
    usage = msg.get("usage") or {}
    model = msg.get("model") or "unknown"
    mid = msg.get("id") or entry.get("uuid")
    dedup_key = (mid, entry.get("requestId"))
    st.hc["assistant_lines"] += 1
    if msg.get("stop_reason"):
        st.hc["stop_reason"] += 1
        if msg.get("stop_reason") == "end_turn" and sess is not None and not side:
            _end_turn(st, sess, ts)
    td = entry.get("thinkingDurationMs")
    if isinstance(td, (int, float)) and day and (mid, "think") not in st.seen_msg_ids:
        st.seen_msg_ids.add((mid, "think"))
        st.hc["thinking"] += 1
        st.thinking.append((day, td))

    if usage and dedup_key in st.seen_msg_ids:
        st.hc["duplicate_lines"] += 1
    if usage and dedup_key not in st.seen_msg_ids:
        st.seen_msg_ids.add(dedup_key)
        i = usage.get("input_tokens", 0) or 0
        o = usage.get("output_tokens", 0) or 0
        cw = usage.get("cache_creation_input_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        p = price_for(model)
        cost = (i * p["in"] + o * p["out"] + cw * p["in"] * 1.25 + cr * p["in"] * 0.10) / 1_000_000
        st.hc["assistant_msgs"] += 1
        if entry.get("effort") and day:
            st.hc["effort"] += 1
            st.efforts.append((day, str(entry["effort"])))
        if day:
            d = st.days[day]
            d["input"] += i; d["output"] += o
            d["cache_read"] += cr; d["cache_write"] += cw
            d["cost"] += cost
            # what caching saved: reads billed at 10% instead of 100%, minus the 25% write premium
            d["cache_savings"] += (cr * 0.90 - cw * 0.25) * p["in"] / 1_000_000
        m = st.models[model]
        m["msgs"] += 1; m["input"] += i; m["output"] += o
        m["cache_read"] += cr; m["cache_write"] += cw; m["cost"] += cost
        pr = st.projects[project]
        pr["input"] += i; pr["output"] += o; pr["cost"] += cost
        if day:
            pr["days"][day]["output"] += o
        if sid:
            pr["sessions"].add(sid)
        if sess:
            sess["output"] += o
            sess["cost"] += cost
            sess["model_cost"][model] += cost
            if sess["api_calls"] == 0:
                sess["first_call_cost"] = cost
            elif cw > cr:
                sess["cache_resets"] += 1
            sess["api_calls"] += 1
        b = st.branches[branch]
        b["msgs"] += 1; b["output"] += o

    # tool calls + lines of code
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tid = block.get("id")
            if tid:
                if tid in st.seen_tool_ids:
                    continue
                st.seen_tool_ids.add(tid)
            name = block.get("name", "?")
            st.tools[name] += 1
            feat = "mcp" if name.startswith("mcp__") else TOOL_FEATURES.get(name)
            if feat and day:
                st.feature_days[feat].add(day)
            if name in READ_TOOLS:
                if day:
                    st.days[day]["reads"] += 1
                if sess:
                    sess["reads"] += 1
            elif name in WRITE_TOOLS:
                if day:
                    st.days[day]["writes"] += 1
                if sess:
                    sess["writes"] += 1
            elif name in CMD_TOOLS:
                if sess:
                    sess["bash"] += 1
            elif name == "Agent":
                inp0 = block.get("input") or {}
                st.subagents[inp0.get("subagent_type") or "unknown"] += 1
            inp = block.get("input") or {}
            st.hc["tool_uses"] += 1
            is_cmd = name in CMD_TOOLS
            cmd = inp.get("command") if is_cmd else None
            kind = classify_cmd(cmd) if is_cmd else None
            if tid:
                st.pending_tools[tid] = {
                    "name": name, "ts": ts, "sid": sid, "side": side, "kind": kind,
                    "family": cmd_family(cmd) if is_cmd else name}
            fp0 = inp.get("file_path") or inp.get("notebook_path") or ""
            if day and is_cmd and isinstance(cmd, str):
                for cat in risky_categories(cmd):
                    st.safety.append((day, cat))
            if day and fp0 and (name in WRITE_TOOLS or name == "Read") and SECRET_PATH_RE.search(fp0):
                st.safety.append((day, "secret_file_touched"))
            if sess is not None and not side:
                sess["seq"] += 1
                if name in WRITE_TOOLS:
                    sess["last_write_seq"] = sess["seq"]
                    if sess["first_edit_s"] is None and ts is not None and sess["start_dt"] is not None:
                        sess["first_edit_s"] = max(0.0, (ts - sess["start_dt"]).total_seconds())
                    if fp0:
                        if day:
                            st.edits.append((day, sid, fp0, project))
                        if TEST_FILE_RE.search(fp0):
                            sess["test_files"] += 1
                elif is_cmd:
                    sess["last_cmd_seq"] = sess["seq"]
                    if kind in ("test", "build", "lint"):
                        sess["last_verify_seq"] = sess["seq"]
                if sess["turn"] is not None:
                    sess["turn"]["steps"] += 1
            loc = 0
            if name in CODE_TOOLS_WRITE:
                loc = count_lines(inp.get("content"))
            elif name in CODE_TOOLS_EDIT:
                loc = count_lines(inp.get("new_string"))
            elif name == "MultiEdit":
                loc = sum(count_lines(e.get("new_string")) for e in inp.get("edits", [])
                          if isinstance(e, dict))
            elif name == "NotebookEdit":
                loc = count_lines(inp.get("new_source"))
            if loc:
                if day:
                    st.days[day]["loc"] += loc
                st.projects[project]["loc"] += loc
                if day:
                    st.projects[project]["days"][day]["loc"] += loc
                st.branches[branch]["loc"] += loc
                if sess:
                    sess["loc"] += loc
                fp = inp.get("file_path") or inp.get("notebook_path")
                if fp:
                    st.files_touched[os.path.basename(fp)] += loc
                    st.projects[project]["files"][os.path.basename(fp)] += loc
