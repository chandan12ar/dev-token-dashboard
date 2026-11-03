#!/usr/bin/env python3
"""Release check: the mechanical half of docs/RELEASE_CHECKLIST.md.

Reads a logs folder the way the dashboard does and prints one PASS / WARN / FAIL line per check:
log format drift, new fields, prices and cross-checks against Claude Code's own totals, versions vs the
compatibility matrix, and the test suite. Exit code 1 if anything FAILs. A WARN needs a human to read it.

Reads only structure and counts; prints no prompt, code or file content. Standard library only.

    python tools/release_check.py [--dir PATH] [--skip-tests]
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import ai_fluency_insights as afi  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MATRIX_PATH = os.path.join(ROOT, "docs", "COMPATIBILITY.md")

_ROW = re.compile(r"^\|\s*(\d+\.\d+\.\d+)\s*\|\s*`?([\w.-]+)`?\s*\|\s*(verified|unverified)\s*\|", re.M)


def parse_matrix(text):
    """-> {version: (adapter, status)} from the Claude Code versions table in COMPATIBILITY.md."""
    return {v: (a, s) for v, a, s in _ROW.findall(text or "")}


def check_matrix(matrix_text, manifest):
    """The matrix page and the baseline manifest must name the same verified versions, and every adapter."""
    rows = parse_matrix(matrix_text)
    verified = {v for v, (_a, s) in rows.items() if s == "verified"}
    base = set((manifest or {}).get("claude_code_versions", []))
    out = []
    if not rows:
        return [(FAIL, "compatibility matrix", "no version rows found in docs/COMPATIBILITY.md")]
    only_doc, only_manifest = sorted(verified - base), sorted(base - verified)
    if only_doc or only_manifest:
        bits = []
        if only_doc:
            bits.append("marked verified in the matrix but not in the baseline manifest: " + ", ".join(only_doc))
        if only_manifest:
            bits.append("in the baseline manifest but not recorded in the matrix: " + ", ".join(only_manifest))
        out.append((FAIL, "compatibility matrix", "; ".join(bits)))
    else:
        out.append((PASS, "compatibility matrix", "matches the baseline manifest (verified: %s)" % ", ".join(sorted(verified))))
    names = {a for a, _p, _f in afi.ADAPTERS}
    missing = sorted(n for n in names if n not in matrix_text)
    if missing:
        out.append((FAIL, "compatibility matrix", "adapter not described in the matrix: " + ", ".join(missing)))
    return out


def check_versions(health, matrix_text):
    seen = health["schema"]["versions_seen"]
    rows = parse_matrix(matrix_text)
    verified = {v for v, (_a, s) in rows.items() if s == "verified"}
    if not seen:
        return [(WARN, "versions", "no Claude Code version found in these logs; nothing to verify")]
    new = [v for v in seen if v not in verified]
    if new:
        return [(WARN, "versions", "not verified yet (not in the compatibility matrix): %s. "
                                   "Follow section A of the release checklist" % ", ".join(new))]
    return [(PASS, "versions", "all versions in these logs are verified: %s" % ", ".join(seen))]


def check_schema(health):
    sc = health["schema"]
    out = []
    if not sc["baseline"]["loaded"]:
        out.append((FAIL, "log format", "baseline manifest not found (schema/claude_code_known.json)"))
    warnings = [f for f in sc["findings"] if f["severity"] == "warning"]
    for f in warnings:
        out.append((FAIL, "log format", "%s [%s]" % (f["title"], f["path"])))
    if not warnings:
        out.append((PASS, "log format", "every field the dashboard reads is present with the expected type"))
    for f in sc["findings"]:
        if f["kind"] == "new_field":
            out.append((WARN, "new fields", "%s: %s%s" % (f["title"], ", ".join(f.get("paths", [])[:5]),
                                                          " ..." if f.get("n_paths", 0) > 5 else "")))
        elif f["kind"] == "type_change_other":
            out.append((WARN, "retyped fields", f["title"]))
        elif f["kind"] == "unverified_version":
            out.append((WARN, "reader", f["title"]))
    unknown = health.get("unknown_entry_types") or []
    if unknown:
        out.append((WARN, "entry types", "not read: " + ", ".join("%s (%d)" % (u["type"], u["n"]) for u in unknown)))
    return out


def check_capabilities(health):
    caps = {c["id"]: c for c in health["capabilities"]}
    out = []
    for cid, name in (("tokens", "tokens and cost"), ("readable", "log lines readable"),
                      ("prices", "model prices"), ("crosscheck", "cross-check with Claude Code")):
        c = caps.get(cid)
        if c is None:
            out.append((WARN, name, "not reported by this version of the dashboard"))
        elif c["status"] == "ok":
            out.append((PASS, name, c["detail"]))
        elif c["status"] == "missing":
            # Nothing to compare against (for example no cost-state lines in these logs): not a failure, not a pass.
            out.append((WARN, name, "could not be checked: " + c["detail"]))
        else:
            out.append((FAIL, name, c["detail"]))
    return out


def run_tests():
    proc = subprocess.run([sys.executable, "-m", "unittest", "test_ai_fluency_insights"], cwd=ROOT,
                          capture_output=True, text=True)
    tail = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()][-3:]
    ran = next((ln for ln in tail if ln.startswith("Ran ")), "")
    if proc.returncode == 0:
        return [(PASS, "test suite", ran or "passed")]
    return [(FAIL, "test suite", " / ".join(tail) or "failed")]


def gather(root, matrix_text, manifest, skip_tests=False):
    st = afi.Scanner(root).build_stats()
    health = afi.build_health(st)
    out = []
    if not health["schema"]["versions_seen"] and not st.parse_errors and not len(getattr(st, "turns", [])):
        out.append((WARN, "logs", "no log entries were read from %s" % root))
    out += check_versions(health, matrix_text)
    out += check_matrix(matrix_text, manifest)
    out += check_schema(health)
    out += check_capabilities(health)
    if st.parse_errors:
        out.append((FAIL, "parse errors", "%d log entries could not be parsed" % st.parse_errors))
    if not skip_tests:
        out += run_tests()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Release check for the AI Fluency Insights dashboard")
    ap.add_argument("--dir", default=afi.default_root(), help="Claude projects log dir (default: your own)")
    ap.add_argument("--skip-tests", action="store_true", help="skip the unit test suite")
    args = ap.parse_args(argv)
    with open(MATRIX_PATH, encoding="utf-8") as f:
        matrix_text = f.read()
    results = gather(args.dir, matrix_text, afi.load_schema_manifest(), args.skip_tests)
    for level, name, msg in results:
        print("%-5s %-28s %s" % (level, name, msg))
    n = {lv: sum(r[0] == lv for r in results) for lv in (PASS, WARN, FAIL)}
    print("\n%d passed, %d to read, %d failed" % (n[PASS], n[WARN], n[FAIL]))
    return 1 if n[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
