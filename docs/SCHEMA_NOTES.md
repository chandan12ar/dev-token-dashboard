# Claude Code Log Schema Notes

**Phase 0 output** of [LOG_RESILIENCE_ROADMAP.md](LOG_RESILIENCE_ROADMAP.md).
Observed structure only: keys and types, never values. Generated with `tools/schema_census.py`.

> **Confidence warning.** The only real log available when this was written was one short session on
> Claude Code **2.1.316** (134 lines, ~1 MB). Everything under "Observed" is true for that sample and
> nothing more. Fields marked *optional* might be always-present in other versions. **Re-run the census
> on logs from several machines and versions before treating any row as settled**, and add the result here.

Re-run: `python tools/schema_census.py --text` (add `--json out.json` to share a structure-only file).

---

## 1. What the documentation says (verified 2026-11-02)

Sources: Claude Code docs, pages *Explore the .claude directory*, *Hooks*, *Monitoring usage*.

| Fact | Detail | Why it matters here |
|---|---|---|
| Transcript location | `~/.claude/projects/<project>/<session>.jsonl`. `CLAUDE_CONFIG_DIR` relocates `~/.claude` | Scanner must honour `CLAUDE_CONFIG_DIR` (see `claude_config_dir()` in the script) |
| **Retention** | Files are deleted once older than `cleanupPeriodDays`. **Default 30, minimum 1; 0 is a validation error** | Confirms the retention risk. A "keep forever" setting does not exist; the option is a large number |
| Managed override | If *managed settings* set `cleanupPeriodDays`, that value wins, and a user cannot raise it | Our "raise the setting" advice will not work for managed machines. Warn accordingly |
| Sweep can pause | If Claude Code cannot safely determine the period, the sweep is paused | The absence of deletion is not guaranteed either way |
| Extra transcript files | `<session>.orphaned-<timestamp>-<suffix>.jsonl` and `<session>.jsonl.superseded-<timestamp>`: *"a previous transcript for the session that Claude Code set aside instead of overwriting or deleting it"* | **Transcripts can be rewritten.** "Append-only" is not guaranteed. See §3 |
| Subagent transcripts | `projects/<project>/<session>/subagents/` in "the same format", removed with the parent | Separate files in a sub-directory; see finding F2 |
| Large tool output | `projects/<project>/<session>/tool-results/` holds spilled outputs | Not `.jsonl`, so not scanned; the transcript refers to them |
| Writes can lag | *"The transcript file is written asynchronously and may lag the in-memory conversation"* (Hooks page) | A live read may miss the latest turn; tolerate and re-read |
| Format stability | Neither page documents the transcript format or promises stability. The hooks page steers hook authors to `last_assistant_message` instead of parsing the transcript | Confirms the format is internal. Treat as undocumented |
| Disable logging | `CLAUDE_CODE_SKIP_PROMPT_HISTORY` skips writing transcripts; `--no-session-persistence` for `-p` runs | "No data" can be deliberate. Data Health should say so rather than imply breakage |
| Kept indefinitely | `history.jsonl` (typed prompts), `stats-cache.json` (aggregated token and cost counts behind `/usage`) | **`stats-cache.json` is an independent, long-lived source** for cross-checks and for totals older than the transcripts |
| Plaintext | Transcripts are unencrypted; they can contain `.env` contents, command output, the system prompt and tool definitions | Sets the privacy bar for any archive (roadmap §4) |
| Hooks input (stable, documented) | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `effort`, `hook_event_name`, plus `tool_name` / `tool_input` / `tool_use_id` on tool events. 33 event names incl. `SessionStart`, `Stop`, `PostToolUse`, `PreCompact`, `SubagentStop` | A documented contract exists for a hook-driven event journal (option C) |
| OpenTelemetry (documented) | Metrics `claude_code.token.usage`, `cost.usage`, `lines_of_code.count`, `code_edit_tool.decision`, `active_time.total`. Events `api_request` (tokens, cost, duration), `tool_result`, `tool_decision`, `user_prompt`, `retention_sweep`. Opt-in via `CLAUDE_CODE_ENABLE_TELEMETRY=1`; exports to a local OTLP endpoint or Prometheus on `localhost:9464` | Option D is real and local-capable, but the user must enable it and it needs a receiver; coarser than transcripts |

Not verified: whether `--resume` copies earlier messages into the new file (docs read did not say). Keep dedupe by
`uuid`/message id regardless.

---

## 2. Observed on Claude Code 2.1.316 (one session)

### Entry types

| `type` | n | Has `version` | Handled by `parse_entry` | Notes |
|---|---|---|---|---|
| `assistant` | 49 | yes | yes | tokens, model, tool calls, thinking |
| `user` | 32 | yes | yes | prompts and `tool_result` blocks |
| `attachment` | 20 | yes | partly (`_parse_aux_entry`) | embeds tool definitions, hook output, context |
| `system` | 1 | yes | partly | `subtype=stop_hook_summary` |
| `atis-latch` | 12 | **no** | no (ignored) | `{atis, sessionId}`; meaning unknown |
| `last-prompt` | 11 | **no** | no (ignored) | `{lastPrompt, leafUuid, sessionId}`; **holds a prompt's text** |
| `tools-used` | 7 | **no** | no (ignored) | `{tools[], sessionId}` |
| `queue-operation` | 4 | **no** | no (ignored) | `operation=enqueue/dequeue`, `content`, `timestamp` |

(`ai-title`, which the dashboard uses for session titles, did not appear in this sample, so the census cannot
confirm its current shape. `permission-mode` appeared only as a `permissionMode` field value on `user` entries here.)

### Facts that shape the design

- **Several types have no `version`, no `uuid` and no `timestamp`** (`atis-latch`, `last-prompt`, `tools-used`).
  They are repeated state snapshots (12, 11 and 7 copies in one short session). An archive cannot dedupe them by
  `uuid`; it needs a content hash plus file position.
- **Keys generated at runtime exist.** `wireToolInputs.<toolu_id>` and `wireIngestContext.<toolu_id>` use tool-call ids as
  object keys. A naive "new field detected" check would raise a new alert on every tool call. The sentinel **must
  normalise id-like keys** to a placeholder (the census already does).
- **Embedded JSON Schemas.** `attachment.entries[].input_schema` carries whole tool definitions; hundreds of derived paths
  with no meaning for us. The sentinel treats `input_schema`, `wireToolInputs` and `wireIngestContext` as **opaque**.
- **Tool inputs appear twice** on assistant entries: in `message.content[].input` and echoed in `wireToolInputs`
  (flag `attachment.echoWireToolInputs`). Anything that walks the whole entry rather than named fields can double count.
  Log lines are large (~7.5 KB per line on average in this sample); do not extrapolate monthly volume from one sample.
- **PII-bearing paths exist** in plain structure, for example `attachment.context.userEmail`. Redaction in
  `--collect-fixture` must be by value (all strings dropped), not by an allow-list of paths.
- **Mixed-type paths** (same path, two JSON types): `message.content` (string or list), `message.content[].content`,
  `attachment.content`, `parentUuid` (null or string). The parser already branches on `message.content`; the sentinel must
  record mixed types as *known*, not as drift.
- **Token usage shape** (the fields the dashboard depends on): `message.usage.input_tokens`, `output_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens` (all int, always present in this sample). Also present and
  currently unused: `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`,
  `output_tokens_details.thinking_tokens`, `server_tool_use.web_search_requests`, `service_tier`, `speed`,
  `inference_geo`, `iterations[]`. The 5m/1h split would improve cache-cost accuracy (1-hour cache writes are priced
  differently); worth a later look, flagged not verified.
- **Other fields seen on `assistant`:** `requestedModel`, `advisorModel`, `perTurnEffort`, `effort`, `thinkingDurationMs`
  (only on thinking turns), `isSidechain`, `entrypoint`, `gitBranch`, `requestId`.

---

## 3. Findings from running the current scanner (verified with fixtures)

Reproduced in a scratch directory with three files: a normal transcript, an `.orphaned-…jsonl` copy of it, and a
`<session>/subagents/agent-1.jsonl`.

| ID | Finding | Evidence | Severity |
|---|---|---|---|
| **F1** *(fixed in Phase 1)* | Orphaned/set-aside copies (`*.orphaned-*.jsonl`) are scanned as ordinary transcripts. Token totals survive thanks to message-id dedupe, but other counters double count | 2 distinct messages produced `days[...]["messages"] == 3`; `duplicate_lines == 1` | Medium: silent inflation of message, prompt and heatmap counts |
| **F2** *(fixed in Phase 1)* | Subagent transcripts in `<session>/subagents/` are attributed to a project literally named **`subagents`**, because the project is taken from the file's parent directory name | `list(st.projects)` returned `['home/u/proj', 'subagents']` | Medium: wrong per-project tables |
| **F3** *(fixed in Phase 1)* | The "Tokens and cost" Data Health check is always `ok` when any assistant message exists (`_field_status(n, n)`) | Code read, `build_health` | High for trust: the check meant to catch drift cannot fire |
| **F4** *(fixed in Phase 1)* | JSON decode failures are skipped without being counted, and torn-last-line vs mid-file corruption are indistinguishable | Code read, `Scanner.scan` | Medium |
| **F5** *(reported in Phase 1)* | Four entry types in real logs (`atis-latch`, `last-prompt`, `tools-used`, `queue-operation`) are never referenced anywhere in the repo | grep of `*.py`, `*.md`: 0 hits | Info: may hide usable signals, may be noise. Needs a decision, not a fix |

Live-file behaviour: the prefix of the live transcript (first 500,000 bytes) was hashed at the start of this
session and compared again at the end. See §4.

---

## 4. Append-only check on a live log

| When | Prefix hash (first 500,000 bytes, sha256) | File size |
|---|---|---|
| Start of Phase 0 | `7fc663e8…dbde3fd` | 1,084,102 bytes |
| Later in the same session | `7fc663e8…dbde3fd` (identical) | 2,125,798 bytes (file roughly doubled) |

**Result: the live file was append-only over this window** (it doubled in size and the first 500,000 bytes were byte-identical). It does **not** prove Claude Code never rewrites a
transcript (the documented `.superseded-` / `.orphaned-` files say it sometimes does), so checkpoint logic must still
detect truncation and rewrites.

---

## 5. Still open (needs real logs from other machines and versions)

- [ ] Run the census on at least 3 more installs spanning different Claude Code versions; add a column per version.
- [ ] Capture a log containing subagents, compaction (`PreCompact` / `/compact`), plan mode, `ai-title`, and a resumed
      session; record which entry types and fields each adds.
- [ ] Confirm what `--resume` writes (copy of history, new file, or append).
- [ ] Decide what to do about F5 types.
- [ ] Measure monthly log volume and rewrite frequency on a heavily used machine.
