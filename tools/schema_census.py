#!/usr/bin/env python3
"""Schema census for Claude Code transcript logs (Phase 0 of docs/LOG_RESILIENCE_ROADMAP.md).

Walks a logs directory and records, per entry type, every JSON path and the value types seen
there, with counts and the Claude Code versions each path appeared under. It also measures
file sizes, line counts, undecodable lines, and parse speed.

It records STRUCTURE ONLY: keys and types, never values. The output is safe to share.
(The only values kept are the `type`, `version` and `entrypoint` fields, which are
enumerations and not user content.)

Standard library only. Usage:
    python tools/schema_census.py [--dir ~/.claude/projects] [--json out.json] [--text]
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

# Arrays are collapsed to "[]" so a 500-element list is one path, not 500.
MAX_DEPTH = 12

# Some objects are keyed by generated ids (e.g. wireToolInputs.toolu_01Abc...). Each id would
# otherwise become its own "path". Collapse id-like keys to a placeholder.
_ID_KEY = re.compile(r"^(toolu_[A-Za-z0-9]+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-f]{24,})$")


# Subtrees whose inner shape is free-form by design (JSON Schemas of tool definitions, per-tool
# echoed inputs). We record the key itself and its type, but do not descend.
OPAQUE_KEYS = {"input_schema", "wireToolInputs", "wireIngestContext"}


def norm_key(k):
    k = str(k)
    return "<id>" if _ID_KEY.match(k) else k


def tname(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    return "object"


def walk(v, path, out, depth=0):
    """Yield (path, typename) for v and everything below it."""
    out.append((path, tname(v)))
    if depth >= MAX_DEPTH or path.rsplit(".", 1)[-1] in OPAQUE_KEYS:
        return
    if isinstance(v, dict):
        for k, child in v.items():
            walk(child, path + "." + norm_key(k) if path else norm_key(k), out, depth + 1)
    elif isinstance(v, list):
        for child in v:
            walk(child, path + "[]", out, depth + 1)


def content_block_type(entry):
    """For user/assistant entries, the types of message.content blocks (an enumeration)."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ()
    c = msg.get("content")
    if isinstance(c, str):
        return ("<str>",)
    if isinstance(c, list):
        return tuple(sorted({b.get("type", "?") if isinstance(b, dict) else "<nondict>" for b in c}))
    return ()


def run(root):
    t0 = time.time()
    paths = defaultdict(lambda: {"count": 0, "types": Counter(), "versions": Counter()})
    type_counts = Counter()
    type_versions = defaultdict(Counter)
    subtypes = defaultdict(Counter)          # entry type -> subtype/operation enumerations
    block_types = defaultdict(Counter)       # entry type -> content block types
    files = []
    bad_lines = Counter()                    # "torn_last" / "mid_file" / "not_object"
    total_lines = total_bytes = 0
    parse_seconds = 0.0

    for dirpath, _dirs, names in os.walk(root):
        for fn in sorted(names):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().split("\n")
            except OSError:
                continue
            ends_with_newline = lines and lines[-1] == ""
            if ends_with_newline:
                lines.pop()
            total_bytes += size
            rel = os.path.relpath(p, root)
            files.append({"file": rel, "bytes": size, "lines": len(lines),
                          "subagent_path": "subagent" in rel.lower()})
            tp = time.perf_counter()
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                total_lines += 1
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    last = i == len(lines) - 1 and not ends_with_newline
                    bad_lines["torn_last" if last else "mid_file"] += 1
                    continue
                if not isinstance(e, dict):
                    bad_lines["not_object"] += 1
                    continue
                et = str(e.get("type"))
                ver = str(e.get("version") or "?")
                type_counts[et] += 1
                type_versions[et][ver] += 1
                for key in ("subtype", "operation", "level", "permissionMode"):
                    if isinstance(e.get(key), str):
                        subtypes[et][key + "=" + e[key]] += 1
                for bt in content_block_type(e):
                    block_types[et][bt] += 1
                flat = []
                walk(e, "", flat)
                uniq = {(p, t) for p, t in flat if p}     # count once per entry
                for path in {p for p, _ in uniq}:
                    paths[(et, path)]["count"] += 1
                    paths[(et, path)]["versions"][ver] += 1
                for path, tn in uniq:
                    paths[(et, path)]["types"][tn] += 1
            parse_seconds += time.perf_counter() - tp

    return {
        "root": "<redacted>",
        "generated_in_s": round(time.time() - t0, 3),
        "files": files,
        "totals": {"files": len(files), "lines": total_lines, "bytes": total_bytes,
                   "parse_seconds": round(parse_seconds, 4),
                   "lines_per_second": int(total_lines / parse_seconds) if parse_seconds else None},
        "bad_lines": dict(bad_lines),
        "entry_types": [{"type": t, "n": n, "versions": dict(type_versions[t]),
                         "subtypes": dict(subtypes[t]), "content_blocks": dict(block_types[t])}
                        for t, n in type_counts.most_common()],
        "paths": [{"entry_type": et, "path": p, "n": r["count"], "types": dict(r["types"]),
                   "versions": dict(r["versions"])}
                  for (et, p), r in sorted(paths.items())],
    }


def render_text(c):
    t = c["totals"]
    out = ["files=%d lines=%d bytes=%d parse=%.3fs (%s lines/s) bad_lines=%s"
           % (t["files"], t["lines"], t["bytes"], t["parse_seconds"], t["lines_per_second"], c["bad_lines"])]
    by_type = defaultdict(list)
    for r in c["paths"]:
        by_type[r["entry_type"]].append(r)
    for et in c["entry_types"]:
        out.append("")
        out.append("== %s  n=%d  versions=%s" % (et["type"], et["n"], et["versions"]))
        if et["subtypes"]:
            out.append("   enums: %s" % et["subtypes"])
        if et["content_blocks"]:
            out.append("   content blocks: %s" % et["content_blocks"])
        for r in by_type[et["type"]]:
            share = 100.0 * r["n"] / et["n"]
            flag = "" if share >= 99.5 else "  (optional %.0f%%)" % share
            mixed = "  MIXED-TYPE" if len(r["types"]) > 1 else ""
            out.append("   %-60s %-14s%s%s" % (r["path"], "/".join(sorted(r["types"])), flag, mixed))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    ap.add_argument("--json", help="write full census JSON here")
    ap.add_argument("--text", action="store_true", help="print a readable report")
    a = ap.parse_args()
    if not os.path.isdir(a.dir):
        sys.exit("no such directory: " + a.dir)
    c = run(a.dir)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=1)
    if a.text or not a.json:
        print(render_text(c))


if __name__ == "__main__":
    main()
