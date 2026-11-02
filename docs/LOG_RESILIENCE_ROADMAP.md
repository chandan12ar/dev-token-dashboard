# Log Resilience Roadmap

**Status:** Phase 0 done; Phase 1 in progress · **Branch:** `claude/elegant-heisenberg-c7jxms`
**Purpose:** make AI Fluency Insights trustworthy when Claude Code's log format changes, when we re-scan,
and when Claude Code deletes old logs. This is a living document: update the status column as work lands.

Legend: ☐ not started · ◐ in progress · ☑ done · ✖ dropped (say why)

---

## 1. The questions we were asked

| # | Question | Short answer today | Section |
|---|---|---|---|
| Q1 | Claude Code updates change the log format (fields added/removed/renamed). How do we notice, and keep working? | **Only partly.** We count entry types and versions, and a few hand-picked signals, but most drift is silent. | §2 |
| Q2 | After a pause we run again. Do we rescan from scratch or continue where we stopped? | **Neither fully.** Unchanged files are skipped, but a changed file is re-read from byte 0, and *all* stats are recomputed from scratch on every request. Restart = full cold start. | §3 |
| Q3 | Claude Code may delete old logs. Can we keep our own copy in a local DB? | **Yes, and we should.** Today deleted logs silently shrink "all time". | §4 |
| Q4 | Can we define our own standard structure and read logs into it? | **Yes. It is the right design** (an adapter / anti-corruption layer). | §5 |

The premise is correct: the transcript `.jsonl` files are an internal format of Claude Code, not a published
contract. Fields can be added, renamed or dropped between versions. (`docs/COPILOT_DASHBOARD_BUILD_PLAN.md` says the
same about Copilot's logs.) Most changes are probably additive, but we cannot rely on that. **Phase 0 confirmed the docs do not document the transcript format or promise stability** (`docs/SCHEMA_NOTES.md` §1).

---

## 2. Q1 — Detecting format changes (what the code does now)

Verified in `ai_fluency_insights.py`:

| Behaviour | Where | Consequence |
|---|---|---|
| Unknown entry types are dropped with a bare `return` (after being counted in `entry_types`) | `parse_entry`, ~L533 | A new type that carries useful data is invisible unless someone reads the Data Health type list. |
| Missing numeric fields default to 0 (`usage.get("cache_read_input_tokens", 0) or 0`) | ~L666 | If a field is renamed, tokens quietly become 0. No error, no warning. |
| The "Tokens and cost" health check is `_field_status(assistant_msgs, assistant_msgs)` | `build_health`, ~L1900 | n/d is always 1, so this check **can never fail**. It gives false comfort. |
| `json.JSONDecodeError` lines are skipped without being counted | `Scanner.scan`, ~L825 | Mid-file corruption and a half-written last line look identical, and neither is reported. |
| `parse_errors` counts only exceptions inside `parse_entry` | ~L849 | Wrong shape that does not raise (a list where a dict was) goes uncounted. |
| Only ~11 signals are health-checked, all hand-listed | `build_health` | A new/removed field outside those 11 is not noticed. |
| Versions and entry types are shown, but there is no comparison against a baseline | Data Health tab | Needs a person to notice a change by eye. |

What already exists and is good: `version`/`entrypoint` counters, `parse_errors` banner, the Data Health tab, and the
"not in your logs" idea instead of showing a misleading zero. We build on these.

### Proposed design: a drift sentinel

1. **Schema census.** For each entry, record the set of `(entry_type, json_path, value_type)` triples, for example
   `assistant · message.usage.cache_read_input_tokens · int`. Store counts, `first_seen`, `last_seen`, and the Claude Code
   `version` they appeared under. Arrays collapse to `[]` so it stays small.
2. **Known-schema manifest** checked into the repo (`schema/claude_code_known.json`). Each path is tagged
   `required`, `optional` or `ignored`, and says which metrics consume it.
3. **Classify every difference** against the manifest:
   - **new path** (not in manifest): informational. We could be missing a useful signal.
   - **vanished path** (a required path used to appear for version X and no longer does): **warning**.
   - **type change** (int → string, object → list): **warning**.
   - **consumed path missing** (a metric depends on it): **degrade that metric**, do not show 0.
4. **Metric dependency map.** Extend `METRIC_CATALOG` with `requires=[paths]`. When a required path is missing or changed,
   the metric shows "unverified: field X not found in your logs (Claude Code vX.Y)" rather than a number.
5. **Surfaces:** Data Health "Schema changes" panel, a `--schema-report` CLI command, and a quiet banner only for
   warnings (new-field info stays in the panel, to avoid alert fatigue).
6. **`--collect-fixture`:** emits a *structure-only, content-redacted* sample (keys and types, no prompt/code text) that a
   user can send us. This is how we learn about a new format without anyone sharing private transcripts.
7. **Cross-checks against an independent source.** We already read the statusline `rate_limits` file. Comparing our
   token totals against it for the same window is a canary for *meaning* changes (same field name, different semantics),
   which a structure-only check cannot see.
8. **Tests:** golden fixtures per Claude Code version, plus *mutation tests* (rename/drop/retype a field in a fixture and
   assert the sentinel fires and the dependent metric degrades).

**Honest limit:** this detects structural drift and some semantic drift (via cross-checks). It cannot prove the meaning
of every field is unchanged. Say so in the UI.

---

## 3. Q2 — Rescan vs continue (what the code does now)

- `Scanner` keeps `(mtime, size)` per file. Unchanged file → one `stat`, skipped. ✔
- A **changed** file (the live session) is re-read **from byte 0** and its whole entry list replaced.
- `build_stats()` then runs `parse_entry` over **every cached entry of every file**, on every API request, and a second
  time for ranged views (`docs/ARCHITECTURE.md` measures ~1.7 s warm, ~2.4 s for a ranged request).
- All parsed entries are held in memory. Nothing is persisted, so **restarting the dashboard is a full cold start**.
- `NotificationWatcher` takes a separate copy of the entries and replays them with its own state file.

So the honest answer to "does it remember where it stopped?" is: **at file granularity, in memory only; not at line level,
and not across restarts.**

### Proposed design
1. **Byte-offset checkpoints** per file: `(file_id, offset, size, mtime, first-line hash)`. On change, seek to `offset`,
   read only complete lines, advance the offset only past a newline (a torn last line is retried next time, not lost).
2. **Rewrite/truncate detection:** if `size < offset`, or the first-line hash differs, rescan that file from 0 and
   reconcile (do not double count). Never trust "append-only" blindly.
3. **Persist checkpoints** in the local DB (Phase 2) so a restart resumes. Commit events and the checkpoint in **one
   transaction**, so a crash can neither lose nor duplicate data.
4. **Idempotent ingest:** key every event by a stable id (`uuid`, falling back to a content hash), insert-or-ignore.
   `--resume`d or forked sessions can repeat earlier messages across files; dedupe must survive this.
5. **Later (Phase 4):** incremental aggregation, such as per-day rollups, so a request no longer re-parses history.

---

## 4. Q3 — Keeping our own copy so deletion does not erase history

**Problem, verified:** `docs/PROJECT_REPORT.md` §10 notes Claude Code keeps transcripts 30 days by default
(`cleanupPeriodDays`; **verified in Phase 0**: default 30, minimum 1, `0` is rejected, and managed settings can override a user's value). And `Scanner.scan` *drops cached entries for files
that no longer exist*, so when Claude Code cleans up, our "all time" numbers, streaks and adoption score **shrink**.

**Key principle: capture raw first, interpret later.** Deletion is irreversible; every parsing mistake is recoverable *if
the raw line was kept*. That is why the archive comes before the big refactor.

### Options considered
| Option | Pros | Cons |
|---|---|---|
| **A. Copy the `.jsonl` files to a backup folder** | ~20 lines of code, verbatim | No queries, no dedupe, still need to parse everything each time |
| **B. SQLite archive (stdlib `sqlite3`)** | Zero new dependency (keeps our "stdlib only" promise), transactional, queryable, holds checkpoints and census too | Needs schema migrations, size management |
| **C. Hook-driven journal** (Claude Code hooks write our own event stream live) | Hooks have a documented input contract, more stable than transcripts; we already install a statusline hook | Only captures while hooks are installed; misses history; hooks API must be verified |
| **D. Local OpenTelemetry receiver** | Documented, versioned telemetry | Needs user to enable it; coarser than transcripts; **verify what it exposes** |

**Recommendation:** B as the core. Treat C/D as *optional second sources* for cross-checking (§2 item 7), decided after the
Phase 0 spike.

### Rules for the archive (the cavities others will find)
- **Opt-in, with plain language.** Some people or companies set short retention *on purpose* (compliance, secrets).
  Silently keeping a copy defeats that. First run asks; the choice is visible and reversible.
- **It stores sensitive data** (prompts, code, possibly secrets). So: file mode `0600` / user-only ACL, never served over HTTP,
  excluded from backups where possible, a `--purge-archive` command, and a size cap with a visible usage meter.
  Alternative per user: **facts-only mode** (canonical events without raw text), which is more private but cannot be reprocessed.
- **Never delete on source disappearance.** The "drop deleted files" branch becomes "mark source as gone".
- **Ingest must run even when the dashboard is closed**, or cleanup can win the race. Options: ingest at every startup,
  plus the existing statusline hook as a trigger, plus a scheduled task. Cheapest immediate help: **warn on startup** if
  the oldest log is near the retention horizon, and tell the user how to raise `cleanupPeriodDays`.
- **Store timestamps in UTC; compute local day at query time.** Today `day` is baked in at parse time, so travel/DST would
  silently shift history if we persisted it.
- **Store token counts, not dollars.** Cost is computed at parse time from the *current* `PRICING` table; an archive must
  recompute from counts with effective-dated prices, or old months get re-priced silently.
- **Concurrency:** `Restart-Insights` can leave two instances; use WAL mode and a single-writer lock. Windows file locking
  and OneDrive-synced folders need testing.
- **Migrations and recovery:** `PRAGMA user_version`, a tested migration path, and `--rebuild` from raw if the DB is corrupt.
- **Multi-machine:** out of scope now, but include a `machine_id` column so a future merge is possible.

---

## 5. Q4 — Our own standard structure

Yes. Three layers, each replaceable without touching the others:

```
RAW (verbatim lines, append-only)
   |   adapters: one per Claude Code version range
   v
CANONICAL EVENTS (our schema, versioned: prompt, response, tool_call, tool_result, session, ...)
   |   metrics code reads ONLY this layer
   v
DERIVED (days, sessions, scores, advisor) - always rebuildable
```

- **Adapters** translate a raw line into canonical events. Fields we do not understand go into an `extras` JSON column and a
  `schema_census` row. Nothing is lost, and nothing is silently ignored.
- **Reprocessing:** when we learn a new field or fix a bug, a `--reprocess` command replays RAW through the new adapter and
  fills in history. This is the real payoff of keeping raw.
- **Canonical schema is versioned** (`canonical_version`), with a changelog, so a metric change is explainable.
- **Bonus:** this is also the prerequisite for the Copilot dashboard plan. A second adapter, same metrics.
- **Biggest risk:** `parse_entry` is a large function inside a 5.5k-line single file, and tabs read `Stats` directly. Do the
  refactor behind **parity tests** (canonical-based `/api/stats` must equal today's output on fixtures) and change one
  tab's inputs at a time.

---

## 6. Other perspectives: what reviewers will ask

| Who asks | Concern | Where it is handled |
|---|---|---|
| Security / compliance | "You now keep a copy of my prompts?" | §4: opt-in, 0600, purge, facts-only mode, no HTTP exposure |
| Security | Is the dashboard port bound to localhost only? Does any endpoint expose raw archive rows? | Phase 2 review item |
| User | "Why did my numbers change after the update?" | Data lineage line: "N events · adapter vX · 0 unknown fields"; metric changelog |
| User | "Is this alert real or noise?" | Severity tiers, dedupe, acknowledge button |
| User | "Old months got re-priced" | Counts stored, price applied at query time with effective dates |
| Maintainer | "Claude Code shipped a release; is anything broken?" | `--schema-report` plus a release checklist (§8) |
| Maintainer | "Single 5.5k-line file" | Refactor only behind parity tests (Phase 3) |
| QA | How do we test drift? | Golden fixtures per version plus mutation tests |
| Ops / Windows | Locked files, torn writes, mtime granularity, two instances | Torn-line retry, WAL, lock tests |
| Product | "Is this still a read-only tool?" | It now writes to disk. README and SETUP must say so plainly |
| Future | Multi-machine, other assistants | `machine_id`, `source` column, adapter registry |

---

## 7. Phased plan

### Phase 0: Discovery spike (no product change)
| Item | Status |
|---|---|
| Census tool `tools/schema_census.py` (structure only, safe to share; collapses id-like keys, opaque subtrees) | ☑ |
| Run the census on real logs | ◐ one session, one version (2.1.316) only. **Need logs from other machines/versions** |
| Measure: log size per month, rewrite frequency | ◐ one sample; not representative (see SCHEMA_NOTES §2, §4) |
| Verify against current docs: retention default, hook input fields, OTel exposure | ☑ (`SCHEMA_NOTES.md` §1). Resume/fork duplication **not** verified |
| Decide archive shape (B vs B+C) and the opt-in policy | ☐ waiting on the owner (§8) |
| Output: `docs/SCHEMA_NOTES.md` | ☑ |

**Phase 0 findings that change the plan**
- **F1** orphaned transcript copies are scanned and inflate non-token counters; **F2** subagent files are attributed to a project called `subagents`;
  **F3** the tokens health check cannot fail; **F4** undecodable lines are not counted; **F5** four real entry types are unreferenced. Details and evidence: `SCHEMA_NOTES.md` §3.
- Transcripts **can be rewritten** (docs describe `.superseded-` / `.orphaned-` files), so byte-offset checkpoints must detect rewrites.
- `stats-cache.json` (kept indefinitely) is a long-lived independent source for cross-checks.
- Managed settings can pin `cleanupPeriodDays`; the "raise the setting" advice cannot work there.
- Dynamic keys (`wireToolInputs.<id>`) and embedded schemas make a naive new-field detector noisy; normalisation is mandatory.
- Some entry types have no `uuid`/`timestamp` (repeated state snapshots); the archive needs a content-hash key for them.

### Phase 1: Sentinel and quick wins (answers Q1)
| Item | Status |
|---|---|
| **F3** Fix the always-true "Tokens and cost" health check (measure tokens present, not assistant/assistant) | ☐ |
| **F4** Count JSON decode errors separately: torn final line vs mid-file | ☐ |
| **F1** Skip `*.orphaned-*` / `*.superseded-*` set-aside copies (or dedupe them) so counters do not inflate | ☐ |
| **F2** Attribute `<session>/subagents/*.jsonl` to the parent project, not `subagents` | ☐ |
| Count and list unknown entry types and unknown top-level keys (**F5** decision: report, do not parse) | ☐ |
| Startup warning: oldest log age vs retention setting; say when it is pinned by managed settings | ☐ |
| Schema census + `schema/claude_code_known.json` + diff classifier | ☐ |
| `requires=[paths]` on `METRIC_CATALOG`; degrade metrics instead of showing 0 | ☐ |
| Data Health "Schema changes" panel and `--schema-report` | ☐ |
| `--collect-fixture` (structure-only, redacted) | ☐ |
| Mutation tests plus per-version golden fixtures | ☐ |

### Phase 2: Archive and checkpoints (answers Q2 and Q3)
| Item | Status |
|---|---|
| SQLite store: `raw_events`, `sources`, `checkpoints`, `schema_census`, `user_version` migrations | ☐ |
| Opt-in prompt, consent record, `--archive-status`, `--purge-archive`, size cap and meter | ☐ |
| Byte-offset tail reader with torn-line, truncate and rewrite handling | ☐ |
| Idempotent ingest, one transaction for events plus checkpoint | ☐ |
| First-run backfill from existing logs; "gone" marking instead of delete | ☐ |
| Scanner reads archive plus live files; `--rebuild` | ☐ |
| Ingest triggers when the dashboard is closed (decide: hook / scheduled task) | ☐ |

### Phase 3: Canonical model and adapters (answers Q4)
| Item | Status |
|---|---|
| Define canonical event schema v1 and write it down | ☐ |
| Adapter registry by version range; `extras` column | ☐ |
| Parity tests: new path equals old `/api/stats` on fixtures | ☐ |
| Migrate tabs one at a time to read canonical events | ☐ |
| UTC storage, effective-dated pricing, `--reprocess` | ☐ |

### Phase 4: Performance and reach
| Item | Status |
|---|---|
| Incremental rollups so requests stop re-parsing history | ☐ |
| Optional second source (hook journal or OTel) for cross-checks | ☐ |
| `machine_id`, multi-machine merge design | ☐ |
| Second adapter (Copilot) using the canonical model | ☐ |

### Ongoing: governance
| Item | Status |
|---|---|
| Release checklist: on each Claude Code release, run `--schema-report`, update manifest and `SCHEMA_NOTES.md` | ☐ |
| Compatibility matrix: Claude Code versions tested vs adapter version | ☐ |
| README / SETUP / PROJECT_REPORT updated to describe the archive and its privacy stance | ☐ |

---

## 8. Open decisions (need an owner)

1. **Archive default:** opt-in (recommended) or opt-out with a prominent notice?
2. **Raw text:** keep full raw lines (reprocessable, more sensitive) or facts-only (private, not reprocessable)? Offer both?
3. **Scope now:** Claude Code only, or design the canonical schema for Copilot too from day one?
4. **Distribution:** purely personal tool, or will teams/companies run it? (Changes the privacy and compliance bar.)
5. **Where ingest runs when the dashboard is closed:** statusline hook, scheduled task, or accept the gap and just warn?

---

## 9. Changelog

| Date | Change |
|---|---|
| 2026-11-02 | Document created from a read of the current scanner, parser and health code. No code changed. |
| 2026-11-02 | Phase 0: census tool, `SCHEMA_NOTES.md`, docs verified, findings F1 to F5 recorded. Phase 1 items added. |
