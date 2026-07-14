# How the Dev Token Dashboard Works

A 100% local dashboard for Claude Code usage. It consumes **zero tokens** — it
never calls any API. It only reads the session logs Claude Code already saves
on this machine, parses them, and serves a live web page.

## The data source

Claude Code logs every session as JSONL files under:

```
C:\Users\<you>\.claude\projects\<project-dir>\<session-id>.jsonl
```

Each line is one event: a user prompt, an assistant response (with token
usage, model name, timestamp), or a tool call (Write/Edit/etc. with the exact
content written). The dashboard is nothing more than a parser + aggregator
over these files. Nothing leaves the machine.

## The pipeline

1. **Scanner** (`Scanner` class) — walks the projects directory for `*.jsonl`
   files. It caches each file's `(mtime, size)`, so on refresh it re-parses
   only files that actually changed. Fast even with months of history.
2. **Parser** (`parse_entry`) — turns each log line into stats:
   - *Assistant entries*: token usage (input, output, cache read, cache
     write), model, estimated cost. **Deduplicated by message ID** — see
     [TOKEN_COUNTS.md](TOKEN_COUNTS.md) for why this matters.
   - *User entries*: prompt count, prompt length, estimated "human tokens"
     (~4 chars per token), slash commands, interruptions.
   - *Tool calls*: tool usage counts, and lines of code written via
     Write/Edit/MultiEdit/NotebookEdit (counted from the actual content, so
     it's additions, not a git diff).
3. **Web server** — a stdlib `ThreadingHTTPServer` on `http://localhost:8787`
   serving a single-page dashboard. The page fetches `/api/stats` and
   re-renders every 15 seconds.

Requires only Python 3.8+ — standard library, no pip installs. Chart.js is
bundled locally (`chart.umd.min.js`), so the page makes **no external
requests at all** and works fully offline.

## What it shows

**KPI cards**
- Input / output tokens (deduplicated, per API response)
- Cache read tokens + cache hit %
- Estimated API cost (public API pricing; on a subscription the real
  marginal cost is $0 — edit the `PRICING` table in the script to tune)
- Lines of code Claude wrote
- Prompt count + average prompt length
- Sessions + average session duration
- Interruptions (esc / rejections)
- Your typed tokens (estimated) and your **leverage ratio** — Claude output
  tokens per token you typed

**Charts and tables**
- Daily token bars (input / output / cache read)
- "You vs Claude" balance chart (log scale)
- Model usage doughnut + model breakdown table
- Lines of code per day
- Tool usage and slash command usage
- Weekday × hour activity heatmap
- Per-project table (tokens, LoC, prompts, sessions, est. cost)
- Longest prompts and most-edited files

**Time ranges**: 7 / 30 / 90 days / all time, plus a custom from–to date
picker. The selected range applies to *everything* on the Dashboard tab —
charts, tables, and the project deep-dive — and KPI cards show a Δ vs the
previous equal-length window.

**Momentum bar**: current & best day streak, all-time active days, and
optional goal progress bars (configure the `GOALS` dict in the script).

**Exports**: copy a text summary to the clipboard, download the daily table
as CSV, or the full `/api/stats` payload as JSON.

The exact formula behind every one of these numbers is documented in
[METRICS.md](METRICS.md).

## Weekly Report tab

A second tab built for one thing: writing your weekly update in seconds.
All of it comes free from the logs — Claude Code stores an AI-generated
title for every session (`ai-title` entries), so no extra AI calls are
needed.

- **Week selector** — browse any past week with ‹ prev / next ›
- **What you worked on** — session titles grouped by project, with date,
  duration, LoC, and prompt count per session (ready-made standup bullets)
- **KPIs with week-over-week deltas** — sessions, focus hours, output
  tokens, LoC, prompts, building %
- **Exploration vs building gauge** — read-type tool calls (Read, Grep,
  Glob, WebSearch…) vs file-editing calls (Write, Edit…). A 90%-reads week
  was research; a 70%-writes week was implementation.
- **Copy as Markdown** — one click produces a formatted weekly update you
  can paste into Teams, email, or a wiki
- **AI summary (optional)** — a button that pipes the week's bullets
  through `claude -p` for a polished first-person paragraph. This is the
  *only* feature in the whole dashboard that consumes tokens, and only
  when you press it. (Note: the `claude -p` run itself gets logged as a
  tiny new session, so it will appear in your stats.)

## Other developer features

- **Exploration vs building per day** — stacked chart of read vs write
  tool calls on the main dashboard
- **Friction per day** — interruptions (esc) and failed tool calls, to
  spot where the workflow hurts
- **Project deep-dive** — click any row in the Projects table for a modal
  with that project's daily output/LoC chart, its sessions (with titles),
  and its most-edited files
- **Git branch breakdown** — output tokens and LoC attributed per branch
  (uses the `gitBranch` field Claude Code stamps on every log entry)

## Running it

```
python dev_token_dashboard.py               # serves http://localhost:8787
python dev_token_dashboard.py --port 9000   # different port
python dev_token_dashboard.py --dir <path>  # custom logs location
python dev_token_dashboard.py --dump        # print raw stats JSON and exit
```

On Windows, after editing the script, `Restart-Dashboard.bat` stops every
running copy (matched by command line *and* by whoever owns port 8787) and
relaunches one fresh windowless instance, then opens the browser.

## Auto-start at logon

The dashboard can start automatically when you log in, so
http://localhost:8787 is always available. On Windows use either the
built-in `--install-startup` flag (Task Scheduler, needs an Administrator
terminal) or a Startup-folder shortcut pointing at `pythonw.exe` (no admin
needed). macOS and Linux equivalents (cron / systemd) are covered in
[../SETUP.md](../SETUP.md#3-optional-start-automatically-at-logon).
