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
import sys
import time
from collections import Counter, defaultdict

# The walk, id-key collapsing and opaque-subtree rules live in the dashboard itself so the census and the
# sentinel can never disagree about what a "path" is.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ai_fluency_insights as afi  # noqa: E402


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
                uniq = afi.flatten_entry(e)                # count once per entry
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


def write_manifest(census, path):
    """Merge this census into the known-schema baseline at `path` (union; never removes anything)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {}
    m.setdefault("format", 1)
    m["note"] = ("Fields seen in real Claude Code logs. Used by the dashboard's schema sentinel to spot NEW fields. "
                 "Grow it with: python tools/schema_census.py --write-manifest schema/claude_code_known.json")
    vers = set(m.get("claude_code_versions", []))
    types = m.setdefault("entry_types", {})
    for et in census["entry_types"]:
        vers |= {v for v in et["versions"] if v != "?"}
        types.setdefault(et["type"], {})
    for r in census["paths"]:
        cur = set(types.setdefault(r["entry_type"], {}).get(r["path"], []))
        types[r["entry_type"]][r["path"]] = sorted(cur | set(r["types"]))
    m["claude_code_versions"] = sorted(vers)
    m["entry_types"] = {k: dict(sorted(v.items())) for k, v in sorted(types.items())}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, sort_keys=False)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    ap.add_argument("--json", help="write full census JSON here")
    ap.add_argument("--text", action="store_true", help="print a readable report")
    ap.add_argument("--write-manifest", metavar="FILE",
                    help="merge the observed fields into the known-schema baseline FILE (schema/claude_code_known.json)")
    a = ap.parse_args()
    if not os.path.isdir(a.dir):
        sys.exit("no such directory: " + a.dir)
    c = run(a.dir)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=1)
    if a.write_manifest:
        write_manifest(c, a.write_manifest)
        print("manifest updated:", a.write_manifest)
    if a.text or not (a.json or a.write_manifest):
        print(render_text(c))


if __name__ == "__main__":
    main()
