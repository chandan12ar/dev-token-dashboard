# Dashboard Guide — every section explained

A plain-language walkthrough of the whole dashboard: what each section
tells you, where its data comes from, and how to read it. Written so you
can present the dashboard to someone who has never seen it.

For the exact math behind any number, see [METRICS.md](METRICS.md).
For the code architecture, see [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

---

## The one-minute story

> Every time you use Claude Code, it saves a detailed log file on your
> machine — every prompt you typed, every response, every file it edited,
> with timestamps and token counts. This dashboard reads those log files
> (and nothing else), does the math locally, and turns them into charts.
> It never calls any API, costs zero tokens, and nothing leaves the
> machine. It answers three questions: **how much am I using Claude, what
> am I using it for, and how well am I working with it?**

## How the data becomes the dashboard

```
~/.claude/projects/**/*.jsonl        (logs Claude Code already writes)
        │
        ▼
  Scanner  — finds every log file; re-reads only files that changed
        │
        ▼
  Parser   — each log line becomes numbers:
             · assistant messages → tokens, model, est. cost (deduplicated)
             · your prompts       → prompt counts, words, typed-token estimate
             · tool calls         → lines of code, reads vs writes, commands run
             · timestamps         → active time, heatmap, quiet hours, streaks
        │
        ▼
  /api/stats — one JSON payload, rebuilt on demand
        │
        ▼
  The web page — Chart.js renders it; auto-refreshes every 15 seconds
```

One Python file, standard library only, served at `http://localhost:8787`,
bound to your machine only (`127.0.0.1`). The page makes zero external
requests — even Chart.js is bundled locally.

---

# Tab 1: Dashboard

## Header & range bar

- **Live · 0 tokens** pill — a reminder the dashboard itself never spends
  tokens; it only reads files.
- **Range buttons (7 / 30 / 90 days / All time / custom dates)** — every
  card, chart, and table below follows the selected range. The Weekly
  Report tab is the exception: it is always full history.

## Momentum bar

| Item | What it tells you | How it's computed |
|---|---|---|
| **Day streak** | consecutive days you've used Claude Code, counted back from your most recent active day | calendar days that have at least one logged message |
| **Best streak** | your longest-ever run | same, over all history |
| **Active days** | total days you've ever used it | count of days with activity |
| **Goal bars** | progress toward personal targets (daily LoC, token budgets) | optional — set in the `GOALS` dict at the top of the script |

*How to read it:* purely motivational — a GitHub-style contribution
streak for AI-assisted work.

## KPI cards

Each card shows the total for the selected range, plus a ▲/▼ percentage
vs the *previous window of the same length* (e.g. this 30 days vs the 30
days before).

| Card | What it tells you |
|---|---|
| **Input tokens** | text sent *to* Claude — your prompts plus the context (files, history) the agent gathers |
| **Output tokens** | everything Claude generated — answers, code, tool calls |
| **Cache read** | context Claude re-used from cache instead of re-processing. The **hit %** is why long sessions stay cheap — 99% means almost all context was re-used |
| **Est. API cost** | what this usage *would* cost at public API prices. On a subscription plan your real marginal cost is **$0** — this is a "value received" number, not a bill |
| **Lines of code** | lines Claude actually wrote into files (via its Write/Edit tools). Counted from the real content — additions, not a git diff |
| **Prompts** | messages you hand-typed (slash commands and system noise are filtered out) |
| **Sessions** | distinct Claude Code conversations, with average duration |
| **Friction** | times you hit Esc to interrupt + failed tool calls — a "how often did the workflow hurt" counter |
| **Your tokens** | an estimate of tokens *you personally typed* (~4 characters per token) |
| **Leverage** | the headline number: **Claude output tokens ÷ tokens you typed**. 50× means every token you type produces fifty back |

*How to read it:* Leverage is the best single "what does AI assistance
multiply my effort by" number. Friction trending down over weeks means
your prompting is improving.

## Daily tokens (bar chart)

Input / output / cache-read stacked per day. Cache read (grey) dwarfs
the rest by design — that's context re-use, not new spend.

## You vs Claude (line chart)

Tokens you typed vs tokens Claude produced, per day, on a **log scale**
(the gap is usually 10–100×, a linear scale would flatten your line to
zero). The vertical distance between the lines *is* your leverage,
visualised.

## Time & wellness *(v1.2)*

The "am I working sustainably" panel.

| Stat | How it works |
|---|---|
| **Active time** | every message timestamp lands in a 10-minute bucket; active time = buckets × 10 min. Much closer to real hands-on time than session wall-clock (which includes idle time) |
| **Avg per active day** | active time ÷ days that had any activity |
| **Longest focus block** | longest unbroken run of consecutive 10-min buckets in a single day |
| **Quiet hours %** | share of messages between 23:00 and 06:00 (tunable in the script) |
| **Weekends %** | share of messages on Sat/Sun |

The bar chart below shows active minutes per day — bars turn **red** on
days where more than a quarter of activity happened during quiet hours.

*How to read it:* inspired by Anthropic's "Reflect" wellness features.
A red-bar week or a rising quiet-hours share is worth a conversation
with yourself, not a metric to maximise.

## Model usage (doughnut)

Output tokens split by model (Opus, Sonnet, Haiku…). Tells you which
models do your heavy lifting — and, together with the model breakdown
table, where the estimated cost comes from.

## Lines of code / day (line chart)

Code Claude physically wrote into files each day. Spiky by nature —
big features spike it, research days flatline it. Zero on a busy day
just means the day was exploration, not implementation.

## Exploration vs building (stacked bars)

Every tool call is classified:
- **Exploration** = reading tools (Read, Grep, Glob, web search…)
- **Building** = file-editing tools (Write, Edit…)

*How to read it:* neither side is "good". A 90%-exploration day was
research or debugging; a 90%-building day was implementation. What
matters is whether the mix matches what you *meant* to be doing.

## Friction / day (stacked bars)

Interruptions (you pressed Esc because Claude went the wrong way) and
tool errors (a command or edit failed), per day. Spikes usually map to
a specific painful session — hover the day and go look at it.

## Task mix / day *(v1.2, stacked bars)*

Claude Code auto-titles every session. The dashboard keyword-matches
those titles to classify each session: **feature / bugfix / refactor /
docs / explore / other** (with the session's tool mix as a fallback when
the title is ambiguous).

*How to read it:* your work-type profile over time. A month of nothing
but bugfix bars tells a very different story from a feature-heavy one.
It's a heuristic — expect roughly-right, not perfect.

## Tool usage & slash commands (horizontal bars)

Raw counts of which tools Claude used (Edit, Bash, Read…) and which
slash commands you ran. A quick portrait of *how* you and Claude
actually work.

## Activity heatmap

Messages by weekday × hour of day, GitHub-style. Your working rhythm at
a glance — and the quickest way to spot a late-night habit.

## Tables

- **Projects** — tokens, LoC, prompts, sessions, and estimated cost per
  project. **Click any row** for a deep-dive modal: that project's daily
  chart, its sessions with titles, and its most-edited files.
- **Model breakdown** — the doughnut, in numbers.
- **Git branches** — output and LoC attributed per branch (Claude Code
  stamps every log entry with the git branch it ran on).
- **Longest prompts** — your biggest prompts, with previews. Fun, and
  occasionally revealing about what you over-explain.
- **Most-edited files** — where Claude's code actually landed, by LoC.

## Exports

- **Copy** — a text summary of the current range for pasting anywhere
- **CSV** — the per-day table, for spreadsheets
- **JSON** — the entire stats payload, for your own scripts

---

# Tab 2: Weekly Report

Built for one job: **your standup / weekly update writes itself.**
Browse any week with ‹ Prev / Next ›.

## KPI row

Sessions, focus hours, output tokens, LoC, prompts, and building % —
each with a delta vs the previous week.

## What you worked on

Claude Code already stores an AI-generated title for every session
(free — no extra tokens). The dashboard groups them by project with
date, duration, LoC and prompt counts: ready-made standup bullets.

## Exploration vs building & day-by-day charts

The week's read/write split as a doughnut, and output + LoC per weekday.

## AI Fluency report *(v1.2)*

Four 0–100 scores per week, adapted for coding from Anthropic's **4D
AI-fluency framework**, drawn as a radar (orange = this week, blue =
last week) with the underlying numbers and one tip per dimension:

| Dimension | The question it answers | Fed by |
|---|---|---|
| **Delegation** | do you hand Claude real building work, or only ask questions? | % of sessions that shipped code + your leverage ratio |
| **Description** | do your first prompts land, or need a second try? | % of prompts that needed no correction/interruption + prompt length sweet spot |
| **Discernment** | do you actually review and steer the output? | corrections + interruptions (pushing back is *good* here) + tool-error control |
| **Diligence** | do you verify what got written? | % of code-writing sessions that also ran a command (tests, builds, runs) afterwards |

Note the deliberate tension: a "correction" prompt (starting with *no /
wrong / actually / undo…*) **lowers** Description (first prompt didn't
land) but **raises** Discernment (you caught it). That's intentional.

*How to read it:* these are heuristic proxies, not measurements — the
week-over-week *shape* on the radar matters far more than any absolute
number.

## Reflection *(v1.2)*

Each week, the data picks **one question worth sitting with** — e.g.
"22% of this week's activity happened between 11pm and 6am. Is
late-night coding a deliberate choice — or a habit worth questioning?"
The rules fire on high correction rates, heavy quiet-hours use, extreme
exploration/building skew, or low verification; the fallback is
Anthropic's classic: *"What's one thing you want to keep doing yourself,
even if Claude could do it faster?"*

**Discuss with Claude** sends the question plus the week's stats through
`claude -p` for a short reflection with one concrete experiment to try.

## Copy as Markdown & AI summary

- **Copy as Markdown** — one click: the week's sessions, numbers, work
  split, and task mix as a formatted update for Teams / Slack / email.
- **AI summary** — pipes the week through `claude -p` for a polished
  first-person paragraph.

These two buttons (plus "Discuss with Claude") are the **only** features
in the entire dashboard that consume tokens — and only when pressed.

---

# FAQ — questions your team will ask

**"Is this sending our code anywhere?"**
No. It reads local log files, serves a page on `127.0.0.1` only, and
makes zero external requests. Prompts and code never leave the machine.

**"Why does it show a cost when we're on a subscription?"**
It's what the usage *would* cost at public API prices — a value-received
estimate, not a bill. On a subscription the marginal cost is $0.

**"Why are the token counts lower than Claude Code's own panel?"**
The dashboard deduplicates per API response; the official panel counts
some entries more than once. Full analysis in
[TOKEN_COUNTS.md](TOKEN_COUNTS.md) — the dedup'd number is the accurate
one.

**"How accurate is 'lines of code'?"**
It counts lines actually written by Claude's editing tools — real
content, but additions only (no credit for deletions; rewritten lines
count again). Treat it as an activity indicator, not a productivity KPI.

**"Are the fluency scores real measurements?"**
No — honest heuristics, documented formula-by-formula in
[METRICS.md](METRICS.md). Watch the trend, not the value. Everything
that *is* exact (tokens, cache, tools, per-model/project splits) is
exact arithmetic over deduplicated logs.
