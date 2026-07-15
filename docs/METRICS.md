# Metrics & Math — how every number on the dashboard is computed

This document is the formula reference for the dashboard. It explains where
the data comes from, the exact math behind every metric, and what each section
of the UI refers to — so you can trust (or audit) any number you see.

Companion docs:
- [HOW_IT_WORKS.md](HOW_IT_WORKS.md) — architecture and data pipeline
- [TOKEN_COUNTS.md](TOKEN_COUNTS.md) — why our token totals differ from the
  official Claude Code panel (deduplication analysis)

---

## 1. Where the numbers come from

Claude Code writes every session to JSONL files under
`~/.claude/projects/<project-dir>/<session-id>.jsonl`. Each line is one event
with a timestamp, session ID, git branch, and either:

| Event type | What we extract |
|---|---|
| `assistant` message | `usage` block (input / output / cache tokens), model name, tool calls with their full input |
| `user` message | prompt text (or a tool result riding back, including errors) |
| `ai-title` entry | the AI-generated session title (used by the Weekly Report) |

The dashboard re-parses only files whose `(mtime, size)` changed, rebuilds all
aggregates in memory, and serves them at `/api/stats`. **Nothing is ever sent
anywhere** — tracking is purely "read the logs that already exist, every 15
seconds".

All timestamps are converted to **your local timezone** before being bucketed
into calendar days, so "today" means your today, not UTC's.

---

## 2. Token metrics

### Deduplication rule (the foundation of everything)

When Claude streams a response, Claude Code journals **several JSONL lines for
the same API response**, each carrying a copy of the same `usage` object.
Summing every line overcounts tokens ~3×. So each response is counted exactly
once, keyed by:

```
dedup_key = (message.id, requestId)
```

Tool calls are likewise deduplicated by their tool-use `id`. The full analysis
and proof is in [TOKEN_COUNTS.md](TOKEN_COUNTS.md).

### The four token counters

From each unique `usage` block:

| Metric | Field | Meaning |
|---|---|---|
| **Input tokens** | `input_tokens` | fresh (non-cached) tokens sent to the model |
| **Output tokens** | `output_tokens` | tokens the model generated (text, code, tool calls, thinking) |
| **Cache read** | `cache_read_input_tokens` | context replayed from prompt cache — the bulk of real volume |
| **Cache write** | `cache_creation_input_tokens` | tokens written into the cache for future reuse |

### Cache hit rate

```
cache_efficiency = cache_read / (cache_read + input) × 100
```

The share of context that came from cache instead of being re-processed at
full price. High (95%+) is normal and good — it's why long Claude Code
sessions stay cheap.

---

## 3. Estimated API cost

Per unique response, using the `PRICING` table at the top of the script
(USD per 1M tokens, editable):

```
cost = ( input  × price_in
       + output × price_out
       + cache_write × price_in × 1.25     # cache writes cost 25% extra
       + cache_read  × price_in × 0.10 )   # cache reads cost 10% of input
       / 1,000,000
```

The price row is selected by substring match on the model name
(`opus`, `sonnet`, `haiku`, `fable`); unknown models fall back to
Sonnet-class pricing.

> **Important:** this is *"what this usage would cost at public API
> pay-as-you-go prices"*. On a Pro/Max subscription your real marginal cost
> is **$0** — the card exists to show the value you're extracting, not a bill.

---

## 4. Human-side metrics

### Prompts

A "prompt" is a user message with real typed text. Excluded so the count
means *things you actually asked*:

- slash-command invocations (`<command-name>…` blocks) — counted separately
  in the **Slash commands** chart
- local command output blocks
- interruption markers (counted as interruptions instead)
- tool results riding back on user-type entries

`avg_prompt_lines` / `avg_prompt_words` are simple means over the prompts in
the selected range.

### Your tokens (estimated)

There is no tokenizer in the loop, so typed tokens are estimated with the
standard ~4-characters-per-token heuristic:

```
human_tokens = max(1, len(prompt_text) / 4)   per prompt, summed
```

### Leverage ratio

The headline "how much am I getting out of this" number:

```
leverage = claude_output_tokens / your_typed_tokens
```

A leverage of 200× means every token you typed produced two hundred tokens of
Claude output in return. Because `human_tokens` is an estimate, treat
leverage as an order-of-magnitude indicator, not a precise figure.

---

## 5. Lines of code (LoC)

Counted from the **actual content** of file-writing tool calls — not a git
diff. For each deduplicated tool call:

| Tool | Counted text |
|---|---|
| `Write` / `Create` | `content` |
| `Edit` / `StrEditReplace` | `new_string` |
| `MultiEdit` | sum of every edit's `new_string` |
| `NotebookEdit` | `new_source` |

```
loc = newline_count(text) + 1
```

This measures **lines Claude wrote**, i.e. additions/replacements. It does
not subtract deletions, and rewriting the same function twice counts twice.
The same numbers feed the per-project, per-branch, and most-edited-files
breakdowns (files are keyed by basename).

---

## 6. Activity & flow metrics

### Exploration vs building

Every tool call is classified into one of two buckets:

- **Exploration (reads):** `Read`, `Grep`, `Glob`, `LS`, `WebFetch`,
  `WebSearch`, `NotebookRead`, `TodoRead`, `ToolSearch`
- **Building (writes):** `Write`, `Create`, `Edit`, `StrEditReplace`,
  `MultiEdit`, `NotebookEdit`

```
building % = writes / (reads + writes) × 100
```

A 90%-exploration day was research/debugging; a 70%-building day was heads-
down implementation. Neither is "better" — the split just tells you what kind
of week it was.

### Friction

```
friction/day = interruptions + tool_errors
```

- **Interruptions** — user messages containing `[Request interrupted` (you
  pressed Esc or rejected an action)
- **Tool errors** — tool results flagged `is_error` (failed commands, bad
  edits, permission denials)

Spikes here show where the workflow hurt.

### Sessions

A session = one distinct `sessionId`. Its duration is
`last_timestamp − first_timestamp` within that session (wall-clock span, so
it includes thinking/idle time between messages — read "avg session" as
*engagement span*, not pure compute time). When a time range is selected,
sessions are included by their **start day** so the session KPIs stay
consistent with the token KPIs beside them.

### Activity heatmap

Message count bucketed by `(weekday, local hour)` over the selected range.
Darker cell = more messages in that hour slot.

---

## 7. Time ranges, deltas, streaks & goals

### Range filtering

The 7 / 30 / 90-day buttons keep the **last N active days** (calendar days
that have any activity). The custom from–to picker filters by exact calendar
dates. Either way the server rebuilds a range-scoped stats object, so **every
card — charts, tables, longest prompts, project modal — respects the range**,
not just the headline KPIs. The Weekly Report tab always uses full history.

### Period-over-period deltas (▲ / ▼ on KPI cards)

For a rolling N-day window, the comparison window is the N active days
immediately before it:

```
current = last N active days          previous = the N days before those
delta % = round((current − previous) / previous × 100)
```

Shown for output, input, cache read, cost, LoC, prompts, sessions, and your
tokens. Hidden when there's no previous window (custom ranges / all-time).

### Streaks

Over all-time active days:

- **Current streak** — consecutive calendar days counted backwards from the
  most recent active day (a gap of one full day breaks it)
- **Best streak** — longest consecutive run anywhere in history
- **Active days** — total calendar days with any activity

### Goals (progress bars)

Set personal targets in the `GOALS` dict at the top of the script:

```python
GOALS = {
    "daily_loc": 500,        # target lines of code / day
    "daily_tokens": 0,       # output-token budget / day   (0 = hide bar)
    "weekly_tokens": 0,      # output-token budget / week  (0 = hide bar)
}
```

```
progress % = min(100, current / goal × 100)
```

"Today" is your most recent active day; "this week" is the last 7 active
days of output. The bar changes color when you're over target. These are
**your** targets — they have nothing to do with Anthropic's plan limits.

---

## 8. Weekly Report math

Weeks are **ISO weeks (Monday–Sunday)**, keyed like `2026-W28`.

- **Focus hours** = Σ session durations for sessions starting that week ÷ 60
- **Week-over-week deltas** = same percentage formula as above, vs the
  previous ISO week
- **Building %** = the exploration-vs-building formula over the week's tool
  calls
- **What you worked on** = the AI-generated session titles Claude Code
  already stores (`ai-title` entries), grouped by project — zero extra tokens
- **AI summary** = the only token-consuming feature: it pipes the week's
  bullets through `claude -p` locally, only when you press the button (capped
  at 50 session bullets to keep the prompt bounded)

---

## 9. Time & wellness (v1.2)

### Active time

Each message's timestamp is dropped into a **10-minute bucket**
(`hour × 6 + minute ÷ 10`, per local calendar day). Active time =
`buckets × 10 min`. A lone message therefore counts as 10 minutes; a
gap longer than ~10 minutes stops counting. This is deliberately closer
to "time you actually spent" than session wall-clock span.

- **Active time in range** = Σ daily bucket counts × 10
- **Avg per active day** = active time ÷ days with ≥ 1 message
- **Longest focus block** = longest run of *consecutive* buckets in a single
  day × 10 (does not span midnight)

### Quiet hours & weekends

- **Quiet-hours share** = messages with local hour ≥ 23 or < 6, ÷ all
  messages in range. Tune `QUIET_START` / `QUIET_END` at the top of the
  script. Days where > 25 % of messages fall in quiet hours are drawn in
  red on the active-minutes chart.
- **Weekend share** = messages on Sat/Sun ÷ all messages in range.

## 10. Task-type classification (v1.2)

Every session is bucketed into **feature / bugfix / refactor / docs /
explore / other** by keyword-matching its AI-generated title (bugfix
keywords win over refactor, then docs, feature, explore). Sessions with
no title signal fall back to tool mix: wrote code → feature; only
reads → explore. It's a heuristic — expect roughly-right, not perfect.

## 11. AI Fluency report (v1.2)

Four 0–100 heuristic scores per ISO week, adapted for coding from
Anthropic's 4D AI-fluency framework (Delegation, Description,
Discernment, Diligence). All computed locally:

| Dimension | Formula |
|---|---|
| **Delegation** | `50 × (code sessions ÷ sessions) + 50 × min(1, leverage ÷ 50)` — are you handing Claude real building work, not just questions? |
| **Description** | `65 × one-shot rate + 35 × sweet-spot factor`. One-shot rate = `1 − (corrections + interruptions) ÷ prompts`. Sweet spot = avg prompt of 8–150 words scores 1.0, shorter/longer decays. |
| **Discernment** | `50 × engagement + 50 × error control`. Engagement = corrections + interruptions reaching 5 % of prompts scores 1.0 (pushing back is *good* — it means you review output). Error control = `1 − tool errors ÷ edits`. |
| **Diligence** | `% of code-writing sessions that also ran ≥ 1 Bash/PowerShell command` — a proxy for "verified the work" (tests, builds, runs). |

**Corrections** are prompts that *start* with correction language
("no", "wrong", "actually", "that's not", "undo", "revert",
"you missed"…, case-insensitive regex `CORRECT_RE` in the script).
Note the deliberate tension: corrections lower Description (the first
prompt didn't land) but raise Discernment (you caught it).

### Reflection question

One data-backed question per week, picked deterministically (rotating by
week index) from whichever rules fire: correction rate > 8 %,
quiet-hours share > 20 %, exploration share > 65 %, building share
> 85 %, verification rate < 40 % — with Anthropic's classic fallback:
*"What's one thing you want to keep doing yourself, even if Claude could
do it faster?"*. **Discuss with Claude** pipes the question + the week's
stats through `claude -p` (token-costing, button-only, like AI summary).

---

## 12. Known approximations (honest accounting)

| Metric | Approximation |
|---|---|
| Est. cost | public API list prices; $0 real cost on subscription plans |
| Your tokens / leverage | ~4 chars-per-token heuristic, no real tokenizer |
| LoC | additions only; no deletion credit; rewrites count again |
| Session duration | wall-clock span including idle time |
| 7/30/90-day ranges | last N *active* days, not strict calendar windows |
| Messages | meta entries, command output and interruptions are filtered — so the count is lower than the official panel's |
| Active time | 10-min buckets; an isolated message = 10 min; focus blocks don't span midnight |
| Task types | keyword heuristics on AI titles; ambiguous sessions land in "other" |
| Fluency scores | heuristic proxies, not measurements — trends week-over-week matter more than absolute values |
| Corrections | only catches corrections at the *start* of a prompt |
| Task mix / day chart | built from the 300 most recent sessions; very old days in long ranges may undercount |

Everything else — token totals, cache numbers, tool counts, per-model and
per-project splits — is exact arithmetic over deduplicated log entries.
