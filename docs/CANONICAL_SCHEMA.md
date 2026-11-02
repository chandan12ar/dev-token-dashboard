# Canonical Record Schema (v1)

How the dashboard turns Claude Code's raw log lines into its own stable format, and what to do when Claude Code
changes. Phase 3 of [LOG_RESILIENCE_ROADMAP.md](LOG_RESILIENCE_ROADMAP.md). The raw format is described in
[SCHEMA_NOTES.md](SCHEMA_NOTES.md); this page describes our side of the boundary.

## 1. The idea

```
raw JSON line  --adapt_entry-->  Record (canonical v1)  --project_record-->  Stats  -->  tabs
 (Claude Code's                   normalised, versioned,                      (derived,
  internal format)                one adapter per format)                      rebuilt every scan)
```

- **Only the adapter knows the raw format.** `project_record`, the notification watcher and everything downstream read
  Record fields, never raw ones. When Claude Code changes a field, one adapter changes and the metric code does not.
- **Raw is kept, derived is rebuilt.** Records are rebuilt from raw lines on every start, so a better adapter fixes
  history, including history restored from the [archive](../SETUP.md#3c-optional-keep-your-history-local-archive).
- **Nothing unfamiliar is silently dropped.** A top-level field the adapter does not know goes to `Record.extras` and is
  counted (`Stats.extras_keys`, Data Health). A line type nobody reads still produces a Record.

In code: `Record`, `adapt_entry`, `ADAPTERS`, `project_record` in `ai_fluency_insights.py`.

## 2. The Record

One Record per raw line, 1:1 and in the same order (so positions stay stable for the incremental reader).

| Field | Meaning |
|---|---|
| `src` | the raw `type` (`user`, `assistant`, `system`, `attachment`, `ai-title`, `permission-mode`, `cost-state`, or anything else) |
| `sid` | session id |
| `ts_raw` | timestamp exactly as logged (kept because session start/end compare it as text) |
| `ts` | the same instant as an aware **UTC** datetime, or `None`. A timestamp with no zone is read as local time. Local day, hour and weekday are derived from it when projecting, so travel or a time-zone change never rewrites stored data |
| `meta` | Claude Code marked the line as not a real message (`isMeta`) |
| `side` | subagent ("sidechain") work |
| `cwd`, `branch`, `version`, `entrypoint`, `uuid` | as logged |
| `human_input` | the line carries something a person did (not only tool results) |
| `events` | ordered list of events (below) |
| `extras` | top-level raw fields the adapter does not recognise |
| `adapter` | `claude-code-2`, `fallback`, or `unversioned` |

`Record.to_dict()` is JSON-serialisable and carries `canonical_version`.

## 3. Events

Every event is a dict with a `kind`. Order inside a Record matters and is fixed: results before the prompt on a user
line; the response before its tool calls on an assistant line.

| kind | From | Fields |
|---|---|---|
| `title` | `ai-title` | `sid`, `title` |
| `mode` | `permission-mode` | `mode` (or `None`) |
| `hook_run` | `system` with subtype `stop_hook_summary` | |
| `attachment` | `attachment` | `type` |
| `permission_decision` | any user/assistant line with a `permissionDecision` object | `decision`, `source` |
| `tool_result` | each `tool_result` block on a user line | `tool_use_id`, `is_error`, `text` (only kept for failures) |
| `prompt` | user text | `class` = `typed` (with `text`), `command` (with `command`), `local_command`, or `interrupt` |
| `response` | assistant line | `message_id`, `request_id`, `model`, `stop_reason`, `thinking_ms`, `effort`, `usage`, `usage_missing` |
| `tool_call` | each `tool_use` block | `id`, `name`, `command`, `file_path`, `loc`, `subagent_type` |
| `session_totals` | `cost-state` | `sid`, `cost_usd`, `lines_added`, `models` (per model: `input`, `output`, `cache_read`, `cache_write`, `cache_write_1h`, `web_search`, `cost_usd`) |

`response.usage` is `None` when the line has no usage, else `{input, output, cache_write, cache_read, cache_write_1h, web_search}`
as numbers (`cache_write_1h` is the part of `cache_write` that went into the 1-hour cache, billed at 2x, 0 when the log has no
split; `web_search` is the number of web searches). `response.speed` and `response.geo` carry `usage.speed` and
`usage.inference_geo` for pricing (fast mode, US-only inference). Cost rules: [COST_MODEL.md](COST_MODEL.md).
`usage_missing` lists raw counters that were absent or not numeric (they count as 0 and are reported by the
"Tokens and cost" health check). `tool_call.loc` is the number of lines written (Write/Create `content`, Edit
`new_string`, MultiEdit `edits[].new_string`, NotebookEdit `new_source`): the adapter owns each tool's input
conventions, the projector just adds.

## 4. Adapters and versions

```python
ADAPTERS = [("claude-code-2", lambda v: v[0] == 2, _adapt_claude_code_2)]
```

- A line's adapter is chosen by its `version`. The first adapter whose predicate accepts the version tuple wins.
- A line with **no version** (state snapshots such as `last-prompt`) is `unversioned` and read by the newest adapter.
- A versioned line **no adapter claims** is read by the newest adapter but counted as `fallback`. Data Health shows the
  count and the sentinel reports *"Claude Code X has not been verified"* (info). That is the signal that the format may
  have moved under us and nobody has checked yet.

**When Claude Code changes its format**

1. Run `python tools/schema_census.py --text` on logs from the new version; compare with `SCHEMA_NOTES.md` and read
   `--schema-report`.
2. Add `_adapt_claude_code_3` (copy, change what moved) and register it with a version predicate. Keep the old one:
   logs written by old versions still exist, and the archive keeps them forever.
3. Add the new version's lines to the characterization test (`kitchen_sink_logs`) or a new fixture, regenerate the golden
   only for changes you intend (`AFI_REGEN_GOLDEN=1`), and add a line to `SCHEMA_NOTES.md`.
4. Extend `CONSUMED_FIELDS` and `schema/claude_code_known.json` (`--write-manifest`) so drift in the new format is
   detected too.

## 5. Tolerance (a deliberate change from the old parser)

The old parser read raw fields inside the metric code, so a wrongly typed field raised an exception half way through
a line and everything that line would have contributed was lost, with the partial updates already applied. The adapter
now reads what it can:

| Raw problem | Result |
|---|---|
| A token counter is not a number | that counter is 0 and listed in `usage_missing`; the other counters count; no parse error |
| `message`, `usage`, `input`, `attachment` is not an object | treated as empty |
| `edits` is not a list, `file_path` is not a string | 0 lines / no path |
| The line is not a JSON object | no Record; counted in `parse_errors` |

Measured against the previous parser on 6,000 randomly type-corrupted lines: 5,764 identical, 230 where the old parser
threw and this one reads what it can, 6 where both reject the line but leave different partial state, and **0** where
the old parser succeeded and this one differs. On 4,000 lines with random keys dropped and on a real session log
(665 lines) the derived Stats were identical.

## 6. Cross-check with Claude Code's own totals

`cost-state` lines are Claude Code's running totals for a session. Measured on one real session, aligned to the point
where each snapshot was written: output tokens matched to 0.1% in both snapshots (0.999), cache writes 98%, cache reads
94%, input tokens 7%, and the lines-added figure matched exactly (578 = 578). Its totals include calls that never appear
in the transcript (a small Haiku model), which is why only **output tokens** are checked: one-sided, because a snapshot
can be older than the end of the log and subagent files are read after the main file. We pass if we count at least 97%
of what Claude Code recorded; counting clearly fewer means we are missing tokens. See `crosscheck()`.

## 7. Pricing by date

`PRICING` holds current rates and applies to every day unless `PRICE_HISTORY` says otherwise:

```python
PRICE_HISTORY["sonnet-5-5"] = [("2026-06-01", {"in": 3.0, "out": 15.0})]   # days before 2026-06-01 use these
```

No history ships, because there are no verified historical prices to put in it. Note what this means today: cost is
re-derived from token counts on every scan, so changing `PRICING` re-prices all history at the new rate.

## 8. Not done, and why

- **A persisted canonical store** (records in the archive's SQLite). Records are rebuilt from raw on start: a cold start
  reads, adapts and projects 63 MB of logs in about 1.4 s (the previous parser: 1.6 s) and a warm refresh takes about
  0.09 s (previously 0.11 s), so persistence would add a migration burden without a measured need. Revisit with Phase 4
  if start-up time becomes a problem.
- **Dropping raw lines from memory after adapting.** Raw lines are still held for the schema census and
  `--collect-fixture`; everything else reads Records. Peak memory is 169 MB for 63 MB of logs (the previous parser: 161 MB),
  so dropping raw lines is the obvious memory saving for Phase 4.
- **Cost accuracy.** Resolved: the earlier 18% gap was the price model, not the logs. See [COST_MODEL.md](COST_MODEL.md).
