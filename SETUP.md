# Setup Guide

Get the Dev Token Dashboard running on your machine in under two minutes.

## Prerequisites

- **Python 3.8+** — check with `python --version`. Nothing else: the
  dashboard uses only the standard library, no `pip install` needed.
- **Claude Code** installed and used at least once (the dashboard reads the
  session logs Claude Code writes locally; with no logs there is nothing to
  show).
- Any OS — Windows, macOS, or Linux. Auto-start instructions differ per OS
  (see below); everything else is identical.

## 1. Get the code

```
git clone <this-repo-url>
cd dev-token-dashboard
```

(or just download `dev_token_dashboard.py` and `chart.umd.min.js` into a
folder — those two files are the entire app.)

## 2. Run it

```
python dev_token_dashboard.py
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
python dev_token_dashboard.py --dir "D:\path\to\.claude\projects"
```

### Options

```
python dev_token_dashboard.py --port 9000      # different port
python dev_token_dashboard.py --no-browser     # don't auto-open the browser
python dev_token_dashboard.py --dump           # print raw stats JSON and exit
```

## 3. Optional: start automatically at logon

### Windows — option A: Task Scheduler (needs an Administrator terminal)

```
python dev_token_dashboard.py --install-startup     # install
python dev_token_dashboard.py --uninstall-startup   # remove
```

### Windows — option B: Startup folder (no admin needed)

Create a shortcut in your Startup folder pointing at `pythonw.exe` (the
console-less Python), with the script path as argument. In PowerShell:

```powershell
$pyw = (Get-Command pythonw.exe).Source
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$([Environment]::GetFolderPath('Startup'))\Dev Token Dashboard.lnk")
$lnk.TargetPath = $pyw
$lnk.Arguments = '"C:\full\path\to\dev_token_dashboard.py" --no-browser'
$lnk.Save()
```

Delete the shortcut (`Win+R` → `shell:startup`) to remove auto-start.

### macOS

```
crontab -e
# add:
@reboot /usr/bin/python3 /full/path/to/dev_token_dashboard.py --no-browser
```

(or create a `launchd` user agent if you prefer.)

### Linux (systemd)

```
# ~/.config/systemd/user/token-dashboard.service
[Unit]
Description=Dev Token Dashboard

[Service]
ExecStart=/usr/bin/python3 /full/path/to/dev_token_dashboard.py --no-browser

[Install]
WantedBy=default.target
```

```
systemctl --user enable --now token-dashboard
```

## 4. Optional: AI weekly summaries

The Weekly Report tab has an "AI summary" button that turns your week into
a polished standup paragraph by running `claude -p` locally. It only needs
the **Claude Code CLI on your PATH** (if you can type `claude` in a
terminal, it works). This is the single feature that consumes tokens, and
only when you press the button — everything else is free forever.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Log directory not found` | You haven't used Claude Code on this machine yet, or logs are elsewhere — pass `--dir` explicitly. |
| Port already in use | Another instance is running (check http://localhost:8787), or pass `--port 9000`. |
| Page loads but charts are empty | Hard-refresh (`Ctrl+F5`); if you deleted `chart.umd.min.js` the page falls back to the CDN and needs internet once. |
| Task Scheduler install fails | Run the terminal as Administrator, or use the Startup-folder method (option B). |
| Cost card looks wrong | It's an estimate at public API pricing; edit the `PRICING` table at the top of the script. On a subscription your real marginal cost is $0. |

## What next

- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) — architecture, every panel
  explained, and what the Weekly Report tab does
- [docs/TOKEN_COUNTS.md](docs/TOKEN_COUNTS.md) — why our token totals
  differ from the official Claude Code panel, and why ours are the
  accurate ones
