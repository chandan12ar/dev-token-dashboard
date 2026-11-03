# Compatibility matrix

What this dashboard has actually been checked against, and what it only assumes. Claude Code's log format is
undocumented and internal, so "supported" would be an overstatement: the honest word is **verified**, with the evidence.

Update this page whenever the evidence changes (steps in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)).
`tools/release_check.py` fails if the table below and `schema/claude_code_known.json` disagree.

## 1. Claude Code versions

| Version | Reader (adapter) | Status | Checked on | Evidence |
|---|---|---|---|---|
| 2.1.316 | `claude-code-2` | verified | 2026-11-02 | Structure census of one real session, kept as the baseline `schema/claude_code_known.json`; the same session's own `cost-state` totals reproduced by the price rules and the output-token cross-check; 665 lines compared field by field against the previous parser ([CANONICAL_SCHEMA.md](CANONICAL_SCHEMA.md) §5) |
| 2.1.317 | `claude-code-2` | verified | 2026-11-03 | 133 assistant and 69 user lines from one session (Claude Code updated from 2.1.316 to 2.1.317 part-way through it, so the two versions were compared in the same log). All 15 fields the dashboard reads are present with the expected types (the 11 that every entry should carry: the 8 on assistant lines are on 133 of 133, the 3 on user lines on 69 of 69); no field it reads changed type; `tools/release_check.py` shows no FAIL. See the scope note below |

**What "verified" covers.** For both rows: the fields the dashboard reads are present with the expected types, and the
numbers built on them agree with Claude Code's own totals. It does **not** mean every feature was exercised.
- 2.1.316: the original baseline session, plus the 978-line first half of the session used for 2.1.317, which included a
  compaction and MCP tool calls.
- 2.1.317: **not exercised** in the 262 lines: compaction (`/compact`), subagents (`isSidechain`), MCP tool calls, and
  `cost-state` lines. `cost-state` lines carry no version, so the price and cross-check results cover the whole mixed
  session, not 2.1.317 alone. The baseline manifest is a union of both versions, so a field seen only in 2.1.316
  (for example `compactMetadata`) is known but not confirmed to be unchanged in 2.1.317.
- New in 2.1.317, not read by the dashboard: richer `toolUseResult` for edits (`structuredPatch`, `oldString`,
  `newString`, `replaceAll`), `toolUseResult.persistedOutputPath` / `persistedOutputSize`, `attachment.changed` and
  `attachment.reason`. Three fields became nullable or wider: `system.parentUuid` (can be null),
  `assistant message.diagnostics` (null or object), `attachment.content` (also an object).

Every other version is **unverified**. What that means in practice:

| Situation | How the dashboard reads it | What tells you |
|---|---|---|
| Any other `2.x.y` | by the `claude-code-2` reader, on the assumption the format did not move | nothing by version; the sentinel compares each version's *structure* with what the dashboard reads and raises a warning if a field it needs is missing, vanished or retyped |
| A major version with no reader (`1.x`, `3.x`, ...) | by the newest reader, counted as `fallback` | Data Health shows the fallback line count and an "X has not been verified" finding |
| No `version` field (state snapshots) | by the newest reader, counted as `unversioned` | nothing: these lines carry no metrics |

So a new minor version is not announced. Drift is announced when it changes a field the dashboard reads. Everything
else, such as a new field or a changed meaning that keeps the same type, is only caught by the checks in the
[release checklist](RELEASE_CHECKLIST.md) (new-field report, `cost-state` cross-checks), not by the running dashboard.

Rule for adding a row: run the checklist on logs written by that version, update the baseline, and record the evidence
here. Never mark a version verified because it "looks the same".

## 2. Environment

| | Status | Notes |
|---|---|---|
| Python 3.13 on Linux | tested | development and the full test suite |
| Python 3.8 to 3.12 | not tested | the script uses only the standard library; no version-specific syntax has been audited |
| macOS | not tested | path handling is standard `os.path`; nothing macOS-specific is used |
| Windows | not tested | untested areas: file locking and OneDrive-synced folders for the archive, owner-only permissions on the archive file (relies on the user-profile ACL), `Restart-Insights.ps1` |
| Browser UI | Chromium (Playwright) | layout and the tile/banner behaviour were checked in headless Chromium; other browsers not tested |

## 3. Data sources

| Source | Status |
|---|---|
| Claude Code transcripts (`~/.claude/projects/**/*.jsonl`, or `$CLAUDE_CONFIG_DIR/projects`) | the only source |
| Copilot or other tools | not supported: no logs to build against ([LOG_RESILIENCE_ROADMAP.md](LOG_RESILIENCE_ROADMAP.md) Phase 4) |
| Prices | Anthropic's published table, read 2026-11-02 ([COST_MODEL.md](COST_MODEL.md)); Sonnet 5.5 and Haiku 5.5 (base tier) reproduced from Claude Code's own recorded cost, every other row from the published page only |
