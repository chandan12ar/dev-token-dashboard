# Setup Guide

Get AI Fluency Insights running on your machine in under two minutes.

## Prerequisites

- **Python 3.8+** — check with `python --version`. Nothing else: the
  dashboard uses only the standard library, so `pip install` below downloads
  nothing; it just installs the app and its `ai-fluency-insights` command.
- **Claude Code** installed and used at least once (the dashboard reads the
  session logs Claude Code writes locally; with no logs there is nothing to
  show).
- Any OS — Windows, macOS, or Linux. Auto-start instructions differ per OS
  (see below); everything else is identical.

## 1. Get the code and install it

```
git clone <this-repo-url>
cd ai-fluency-insights
pip install .
```

Use `pip install -e .` instead if you plan to edit the code (see
[CONTRIBUTING.md](CONTRIBUTING.md)).

## 2. Run it

```
ai-fluency-insights
```

That's it. Your browser opens http://localhost:8787 with your stats. The
page auto-refreshes every 15 seconds, and the parser re-reads only log
files that changed, so it stays fast even with months of history.

The dashboard is 100% local: it binds to `127.0.0.1` only, makes zero
external requests (Chart.js is bundled), and never calls any API — it
costs **zero tokens** to run.

### Where it finds your logs

Claude Code writes session logs to:

| OS | Default location |
|---|---|
| Windows | `C:\Users\<you>\.claude\projects\` |
| macOS / Linux | `~/.claude/projects/` |

The script auto-detects this (it also honors the `CLAUDE_CONFIG_DIR`
environment variable). If your logs live somewhere unusual:

```
ai-fluency-insights --dir "D:\path\to\.claude\projects"
```

### Options

```
ai-fluency-insights --port 9000      # different port
ai-fluency-insights --no-browser     # don't auto-open the browser
ai-fluency-insights --dump           # print raw stats JSON and exit
```

## 3. Optional: start automatically at logon

### Windows — option A: Task Scheduler (needs an Administrator terminal)

```
ai-fluency-insights --install-startup     # install
ai-fluency-insights --uninstall-startup   # remove
```

### Windows — option B: Startup folder (no admin needed)

Create a shortcut in your Startup folder pointing at `pythonw.exe` (the
console-less Python you installed the app into), running the package. In PowerShell:

```powershell
$pyw = (Get-Command pythonw.exe).Source
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$([Environment]::GetFolderPath('Startup'))\AI Fluency Insights.lnk")
$lnk.TargetPath = $pyw
$lnk.Arguments = '-m ai_fluency_insights --no-browser'
$lnk.Save()
```

Delete the shortcut (`Win+R` → `shell:startup`) to remove auto-start.

### macOS

```
crontab -e
# add:
@reboot /usr/bin/python3 -m ai_fluency_insights --no-browser
```

(or create a `launchd` user agent if you prefer.)

### Linux (systemd)

```
# ~/.config/systemd/user/ai-fluency-insights.service
[Unit]
Description=AI Fluency Insights

[Service]
ExecStart=/usr/bin/python3 -m ai_fluency_insights --no-browser

[Install]
WantedBy=default.target
```

```
systemctl --user enable --now ai-fluency-insights
```

## 3b. Optional: accurate plan-usage numbers and toast notifications

By default the "Plan usage limits" card on the dashboard shows a rough
local estimate. To get Anthropic's real 5-hour/7-day numbers (and, on
Windows, toast pop-ups as you cross usage milestones), run:

```
ai-fluency-insights --setup-notifications
```

It wires up a Claude Code `statusLine` hook for you and shows exactly
what it's about to change before writing anything. Preview without
changing anything with `--setup-notifications --dry-run`. If you already
have a custom `statusLine` script, it won't touch it — it'll tell you
what to add instead. See
[docs/DASHBOARD_GUIDE.md#plan-usage-limits](docs/DASHBOARD_GUIDE.md#plan-usage-limits)
for how this works and why the number can lag by a few minutes when idle.

Toast notifications are Windows-only for now; on macOS/Linux this sets
up the accurate numbers only.

## 4. Optional: AI weekly summaries

The Weekly Report tab has an "AI summary" button that turns your week into
a polished standup paragraph by running `claude -p` locally. It only needs
the **Claude Code CLI on your PATH** (if you can type `claude` in a
terminal, it works). This is the single feature that consumes tokens, and
only when you press the button — everything else is free forever.

## Custom settings

Pricing, personal goals and notification thresholds have sensible defaults, so
most people never touch them. To change them — especially in the packaged
`.exe`, where there is no source to edit — drop a `settings.json` in your
per-user data directory. Only the keys you list are changed; everything else
keeps its default, and a missing or malformed file is ignored.

Data directory:

| OS | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\AIFluencyInsights\settings.json` |
| macOS | `~/Library/Application Support/AIFluencyInsights/settings.json` |
| Linux | `~/.local/state/ai-fluency-insights/settings.json` |

(Set the `AFI_DATA_DIR` environment variable to put it somewhere else.)

Example `settings.json` (a copy is in the download as `settings.example.json`):

```json
{
  "pricing": { "opus": { "in": 15, "out": 75 }, "sonnet": { "in": 2, "out": 10 } },
  "goals":   { "daily_loc": 300 },
  "notify":  { "enabled": true, "window_ceiling": 30000000 },
  "adoption":{ "window_days": 28 }
}
```

Restart the dashboard after editing it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Log directory not found` | You haven't used Claude Code on this machine yet, or logs are elsewhere — pass `--dir` explicitly. |
| Port already in use | Another instance is running (check http://localhost:8787), or pass `--port 9000`. |
| Page loads but charts are empty | Hard-refresh (`Ctrl+F5`); if `web/static/vendor/chart.umd.min.js` is missing from the install the page falls back to the CDN and needs internet once. |
| Task Scheduler install fails | Run the terminal as Administrator, or use the Startup-folder method (option B). |
| A new tab says "Not enough data yet" | Flow needs 5 timed turns and Quality needs 3 sessions where Claude edited files, within the selected date range. Widen the range (30d or 90d). My Adoption needs 3 active days in the last 4 weeks. |
| A metric is missing or shows "not in your logs" | Open the **Data Health** tab. It lists which signals your Claude Code version writes (for example permission decisions or thinking time). Missing signals are shown as missing, never as zero. |
| Cost card looks wrong | It's an estimate at public API pricing; override the `PRICING` table via `settings.json` (see Custom settings). On a subscription your real marginal cost is $0. |

## What next

- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) — architecture, every panel
  explained, and what the Weekly Report, My Adoption and Engineering insight
  tabs do
- [docs/DASHBOARD_GUIDE.md](docs/DASHBOARD_GUIDE.md) — a plain-language tour of
  every tab, including how to read the scores
- [docs/TOKEN_COUNTS.md](docs/TOKEN_COUNTS.md) — why our token totals
  differ from the official Claude Code panel, and why ours are the
  accurate ones
