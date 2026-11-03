# Log Resilience Roadmap

**Status:** Phases 0 to 4 done except the items marked below. Open items need owner decisions or real logs from other machines and tools · **Branch:** `claude/elegant-heisenberg-c7jxms`
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
| **F3** Fix the always-true "Tokens and cost" health check: now counts responses with all four token counters and names the missing ones | ☑ |
| **F4** Count JSON decode errors separately: torn final line vs mid-file (mid-file ones feed `parse_errors`; torn ones do not) | ☑ |
| **F1** Skip `*.orphaned-*` / `*.superseded-*` set-aside copies so counters do not inflate; the count is shown in Data Health. **Trade-off:** a set-aside file could hold messages the live file lacks; the Phase 2 archive must keep them raw | ☑ |
| **F2** Attribute `<session>/subagents/*.jsonl` to the parent project, not `subagents` (`project_label()`, also used by the notification watcher) | ☑ |
| Classify entry types as handled / ignored / **new** and show them in Data Health (**F5**: report, do not parse). Unknown top-level *keys* still to do | ◐ |
| "History window" card in Data Health: retention days (from `settings.json`, default 30), earliest day we hold, and a note that managed settings may override. *Changed from a "warning" on purpose: under normal operation the oldest log is always near the limit, so an age warning would be permanently on* | ☑ |
| Schema census per file at scan time + `schema/claude_code_known.json` baseline + classifier (missing / vanished-in-version / type change = warning; new field = info). Id-like and model-name keys collapse, free-form subtrees stay closed | ☑ |
| Consumed-field contract (`CONSUMED_FIELDS`) mapped to catalog metrics; affected metrics are tagged in the catalog and listed in the finding. KPI tiles on Overview and Weekly are marked "Unverified" when a field they read drifted (`TILE_FIELDS`, `affected_tiles`); catalog badges on the other tabs carry the same pill. Checked in a browser with a dropped cache-read field and a dropped output-token field | ☑ |
| Data Health "Log format changes" panel, top-of-page banner on warnings, and `--schema-report` | ☑ |
| `--collect-fixture FILE`: strings replaced, enumerations/model names/built-in tool names/timestamps kept, ids pseudonymised, numbers kept. Field names are kept, so skim before sharing | ☑ |
| Mutation tests (drop / retype / vanish-in-version a field) | ☑ |
| Per-version golden fixtures from other machines | ☐ needs real logs (Phase 0 open item) |
| Cross-check against Claude Code's own `cost-state` totals (new in Phase 0, see `SCHEMA_NOTES.md`) for *meaning* drift | ☑ done in Phase 3 (output tokens) and in the cost work (prices) |

### Phase 2: Archive and checkpoints (answers Q2 and Q3)
Defaults used because the owner decisions in section 8 were not answered: **opt-in, command-line only, raw lines stored, full history**. All are easy to change.

| Item | Status |
|---|---|
| Byte-offset tail reader (`read_tail`): reads only new bytes, advances past full lines only, SHA-1 anchors detect truncation and rewrites | ☑ |
| Scanner extends entries and census incrementally; a rewritten file is replaced, never appended. Property test: a file growing mid-line matches a fresh scan and each byte is read once | ☑ |
| SQLite store (`Archive`): `meta`, `sources` (per file generation), `lines` (zlib), `user_version`, WAL, mode 0600 | ☑ |
| Opt-in: nothing written until `--archive-enable`, which prints the privacy trade-off and asks. Command-line only (the web page cannot enable it) | ☑ |
| `--archive-status`, `--archive-disable`, `--archive-purge` (asks), `--archive-sync` (for a scheduled task), size cap (2 GB) that pauses capture and says so | ☑ |
| Idempotent ingest: lines and checkpoint in one `BEGIN IMMEDIATE` transaction; two processes cannot duplicate lines | ☑ |
| Rewritten file starts a new generation; older generations kept; files that disappear are marked `gone_at`, never deleted | ☑ |
| Set-aside (`.orphaned-`) copies are archived raw but never counted | ☑ |
| First-run backfill from existing logs (runs inside `--archive-enable`) | ☑ |
| Scanner restores deleted files from the archive, per logs folder; a file that reappears is read from disk | ☑ verified on a real log: totals identical after deleting the file |
| Data Health "Local archive" card, History window card aware of the archive | ☑ |
| Fails soft: a corrupt or missing-sqlite archive never breaks the dashboard | ☑ |
| Ingest while the dashboard is closed | ◐ `--archive-sync` plus documented Task Scheduler / cron lines. **Not automatic**: the user must schedule it. A sync at least every week or two is enough because Claude Code deletes only logs older than the retention period |
| `--rebuild` (re-derive from raw) | ☐ belongs with Phase 3 (needs the canonical model) |
| Persisted checkpoints used by the *scanner* so a restarted dashboard skips already-read bytes | ☐ the scanner's checkpoint is in memory; a restart reads everything once. The archive's checkpoints are persisted |
| Compression / size work: 3.4 MB log became 2.1 MB (about 1.6x); block compression gains only ~15% here. Bigger wins would come from storing repeated tool definitions and echoed tool inputs once | ☐ measure monthly volume first |
| Windows: file locking, OneDrive folders, `icacls` privacy of the archive file (POSIX 0600 is tested; Windows relies on the user-profile ACL) | ☐ untested on Windows |

### Phase 3: Canonical model and adapters (answers Q4)
| Item | Status |
|---|---|
| Characterization tests first: a deterministic "kitchen sink" log and a golden snapshot of the exact `Stats` it produces, generated by the **pre-refactor** parser and committed before any change (`GoldenStatsTests`, regenerate with `AFI_REGEN_GOLDEN=1`) | ☑ |
| Canonical schema v1 written down: Record envelope + 10 event kinds, ordering rules ([CANONICAL_SCHEMA.md](CANONICAL_SCHEMA.md)) | ☑ |
| Adapter registry by Claude Code version (`ADAPTERS`), `unversioned` and `fallback` handling, `extras` for unfamiliar top-level fields | ☑ |
| Projector (`project_record`) replaces the raw-reading parser; `parse_entry` kept as a wrapper | ☑ |
| Parity: golden test, plus differential runs against the previous parser: identical on the kitchen sink, a real 665-line session and 4,000 lines with random keys dropped; on 6,000 type-corrupted lines 5,764 identical, 230 tolerated (old parser threw), 6 both reject, **0** regressions. Three projector mutations were caught only by the golden test | ☑ |
| Notification watcher reads Records (no second raw-format reader). Replays identically to the old one on the real log and 3,000 mutated lines (293 and 1,123 plan-window events compared) | ☑ |
| Scanner caches Records (adapt once per line): warm refresh 0.112 s to 0.089 s, cold 1.58 s to 1.36 s on 63 MB; peak memory 161 MB to 169 MB | ☑ |
| UTC in the canonical layer (local day derived when projecting) | ☑ |
| Effective-dated pricing mechanism (`PRICE_HISTORY`); **no history shipped** because there are no verified historical prices | ☑ mechanism only |
| Unverified Claude Code versions surfaced (fallback adapter use: Data Health chip + sentinel info finding) | ☑ |
| Cross-check against Claude Code's own `cost-state` totals (output tokens, one-sided, 3% tolerance); input/cache/cost deliberately not compared | ☑ |
| Tolerance: wrong-typed fields no longer discard a whole line (documented behaviour change) | ☑ |
| Persisted canonical store, `--reprocess`, `--rebuild` | ✖ not needed: records are rebuilt from raw on every start (cold 1.4 s per 63 MB), so "reprocess" happens automatically. Revisit if start-up time becomes a problem |
| Second adapter / second source (Copilot) | ☐ Phase 4. The registry is ready for it, but there is no Copilot log to build against here |
| Per-version golden fixtures from other Claude Code versions | ☐ still needs real logs from other versions |
| **Cost estimate ~18% above Claude Code's own** | ☑ resolved: it was the price model (1.25x writes instead of 2x for the 1-hour cache, 0.10x reads where Sonnet 5.5 and Opus 5.5 charge 0.05x, stale Opus/Fable/Haiku prices). The published rules reproduce Claude Code's own cost to five decimals; the old formula was +17% to +42% high on Sonnet 5.5 (growing with session length) and 9x on Haiku 5.5. Fixed, with a check in Data Health. See [COST_MODEL.md](COST_MODEL.md) |

### Phase 4: Performance and reach
Measured first, on a realistic corpus (60 sessions, 273 MB, 50,340 entries with distinct ids spread over 60 days).
The earlier benchmark was misleading: copies sharing message ids made de-duplication skip most of the work.

| Item | Status |
|---|---|
| Profile before optimising: the per-request projection (0.74 s full + 0.45 s ranged) and the cold scan (9.9 s, 64% of it the schema census) were the costs; payload building was small except `build_adoption` (0.18 s) | ☑ |
| Schema census counts structure *shapes* with a shared path trie instead of walking and regex-matching every field of every line. Exactly equal to the old walk on 3,000 random documents. Cold scan 9.9 s to 7.6 s. A runaway key space is now bounded | ☑ |
| **Incremental projection** with one deterministic projection order; only new records are projected, with an exact full-rebuild fallback. Snapshots are never changed after they are handed out. Idle refresh 0.6 ms (was about 1.2 s); refresh while the newest session grows 0.15 s (about 8x faster) | ☑ |
| Equivalence proved by randomised appends / torn writes / new files / rewrites / deletes over three date windows against a fresh scan, plus four deliberate breakages each caught | ☑ |
| Side effect: results used to depend on the order files were discovered in (a long-running scanner and a fresh one could disagree on de-duplication across files); the fixed projection order removes that | ☑ |
| Raw parsed lines no longer held in memory (re-read for `--collect-fixture`). Peak memory 644 MB to 158 MB | ☑ |
| Background warm-up at start so the first page load does not wait for a long history | ☑ |
| `machine_id` in the archive (random, made once) | ☑ groundwork only, nothing reads it |
| Multi-machine merge | ◐ design note in section 10; **not built**: no second machine to test on, and the semantics depend on owner decisions 1 to 4 |
| Optional second source (hook journal or OpenTelemetry) for cross-checks | ✖ dropped, for now. The in-log `cost-state` totals (Phase 3) already give an independent check with no setup. OpenTelemetry needs the user to run a collector and is coarser than transcripts; a hook journal only captures while installed. Revisit only if `cost-state` proves unreliable across versions |
| Second adapter (Copilot) | ☐ blocked: there is no Copilot log to build against, and `docs/COPILOT_DASHBOARD_BUILD_PLAN.md` itself makes schema discovery on real logs the mandatory first step. The adapter registry and canonical Record are ready for it |
| Older-session growth is still a full rebuild (about 1.3 s on 273 MB): needs checkpoints of the running Stats at the last few files | ☐ not needed for the common single-session case; revisit if two active sessions is a common pattern |
| Idle `/api/stats` still costs about 0.2 s of payload building (mostly `build_adoption`, which depends on today's date) | ☐ low value |
| Cold start of about 6 s on a 273 MB history | ☐ the remaining cost is JSON decoding and adapting every line once |

### Ongoing: governance
| Item | Status |
|---|---|
| Release checklist: on each Claude Code release, run `--schema-report`, update manifest and `SCHEMA_NOTES.md` | ☐ |
| Compatibility matrix: Claude Code versions tested vs adapter version | ☐ |
| README / SETUP / PROJECT_REPORT updated to describe the archive and its privacy stance | ☑ |

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
| 2026-11-03 | KPI tiles on Overview and Weekly show "Unverified" when a log field they read went missing or changed type (`TILE_FIELDS`, `affected_tiles`); tiles stop showing text derived from the suspect number. 5 new tests (260 total); browser-checked. |
| 2026-11-02 | Document created from a read of the current scanner, parser and health code. No code changed. |
| 2026-11-02 | Cost investigation: the 18% gap was the price model. Published rules reproduce Claude Code's own dollar figure exactly on 6 data points; old formula was +17% to +42% (Sonnet) and 9x (Haiku). Model-aware prices, 1h/5m cache writes, per-model cache-hit rates, Haiku tiers, fast mode, US-only multiplier, web search; Data Health "Model prices" check. 18 new tests (255 total). |
| 2026-11-02 | Phase 4: profiled on a 273 MB corpus; incremental projection with a deterministic order (idle refresh 0.6 ms, live refresh about 8x faster), faster census, raw lines out of memory (644 MB to 158 MB), warm-up, archive machine id. 13 new tests (237 total). OpenTelemetry/hook journal dropped, Copilot blocked on logs, multi-machine left as a design note. |
| 2026-11-02 | Phase 3: canonical Record schema v1, adapter registry, projector, watcher on Records, golden characterization tests, cost-state cross-check, unverified-version signal. 26 new tests (225 total). Zero regressions against the previous parser in differential runs. |
| 2026-11-02 | Phase 2: incremental tail reader and opt-in SQLite archive with restore of deleted logs, `--archive-*` commands, Data Health card. 31 new tests (199 total); mutation-checked; verified on a real log. |
| 2026-11-02 | Drift sentinel shipped: per-file census, baseline manifest, Data Health panel + banner, `--schema-report`, `--collect-fixture`, History window card. 16 new tests (168 total). Tested on a real log (clean) and a simulated field rename (detected, banner shown). |
| 2026-11-02 | Phase 1 quick fixes F1 to F4 and entry-type classification shipped with 8 new tests (152 total passing). Live log confirmed append-only over the session. |
| 2026-11-02 | Phase 0: census tool, `SCHEMA_NOTES.md`, docs verified, findings F1 to F5 recorded. Phase 1 items added. |

---

## 10. Design note: combining several machines (not built)

**Goal.** One dashboard showing a person's Claude Code use across their own computers, and, much later, a team view.

**What exists.** The archive keys files by `(logs folder, relative path, generation)` and restores only into the same
folder, so two machines' archives never mix. Each archive now has a random `machine_id`.

**What would be needed**
1. A way to move data: `--archive-export FILE` (lines for one machine) and `--archive-import FILE`, or reading a second
   archive read-only. Moving transcripts moves prompts, code and secrets, so this is a deliberate act, never automatic.
2. Schema v2 of the archive: add `machine` to `sources` and to its uniqueness, and a tested migration (the archive has a
   `user_version`, but no migration has been written yet).
3. Restoring foreign machines' files into their own namespace, not the local logs folder.
4. Identity rules: session ids are UUIDs and message ids are globally unique, so de-duplication already works across
   machines; the same project path on two machines is probably the same project, but that is a choice.
5. UI: a machine filter, and a statement of which machines contribute.

**Decisions only the owner can make** (they change the design, not just the code)
- Personal tool or team use? A team view should aggregate **facts** (counts, days, models), never ship raw transcripts.
- Raw or facts-only export? Raw can be reprocessed later; facts-only is far safer to move.
- Who is allowed to see a combined view, and does the notice shown by `--archive-enable` cover it?

**Why not built now.** There is no second machine here to test on, and building a data-moving feature on guesses about
privacy semantics is the wrong order. Answering the decisions above is the prerequisite.
