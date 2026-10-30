# Packaging: a single Windows .exe (no source, no Python)

This is how to turn the source into **`AIFluencyInsights.exe`** that your
colleagues double-click to run, with no Python install and no access to the
source code. The build uses **Nuitka**, which compiles the Python to machine
code, so the shipped executable cannot be decompiled back into readable
Python.

> What stays visible: the dashboard's web page (HTML/CSS/JS) is sent to the
> browser like any website, so that part is always inspectable. The analysis
> logic — parsing, scoring, every formula — is Python and is compiled.

## 1. One-time setup on a build machine

Use a **Windows** machine (Nuitka builds a Windows `.exe` only on Windows).

```
python --version          # 3.8 or newer
pip install nuitka
```

Nuitka downloads its C compiler automatically on first run, so you don't need
to install one yourself.

## 2. Build

From the repository root:

```
powershell -ExecutionPolicy Bypass -File scripts\windows\build.ps1
```

Open `scripts\windows\build.ps1` first and set `$Company`, `$Product`,
`$Copyright` and (optionally) an `.ico` path at the top — these become the
file's Properties → Details in Windows. The version is read automatically from
`src/ai_fluency_insights/__init__.py`.

The result is **`dist\AIFluencyInsights.exe`** — one self-contained file. The
script runs `--version` at the end to confirm it works.

## 3. Test before you ship

On a **clean** Windows machine (no Python, no repo):

1. Copy `AIFluencyInsights.exe` over and double-click it. The browser should
   open `http://localhost:8787` with the dashboard.
2. From a terminal, check the admin commands show output:
   ```
   AIFluencyInsights.exe --version
   AIFluencyInsights.exe --setup-notifications
   ```
3. Confirm it reads that machine's own logs, and that the Plan-usage card and
   charts render.

## 4. Package for distribution

The simplest unit is a zip the user unzips and runs:

```
AIFluencyInsights/
├── AIFluencyInsights.exe
├── README.txt               (the short "how to run" below)
└── settings.example.json    (optional overrides; see SETUP.md)
```

A short `README.txt` for end users:

```
AI Fluency Insights
-------------------
1. Double-click AIFluencyInsights.exe.
2. Your browser opens http://localhost:8787 with your Claude Code stats.
3. Everything stays on your machine. It never uploads anything and costs
   zero tokens.

Optional, for exact plan-usage % and Windows notifications:
   open a terminal here and run:  AIFluencyInsights.exe --setup-notifications

To change pricing or goals, see settings.example.json.
```

For a proper installer (Start-menu shortcut, uninstaller), use **Inno Setup**
(free) — a `.iss` script wrapping the same files. Ask if you want one written.

## 5. Code signing (important for a wide rollout)

An unsigned `.exe` makes Windows SmartScreen warn every user that the file is
untrusted, and many managed machines block it outright. Before giving this to
~1,000 people:

- Sign `AIFluencyInsights.exe` with your organization's **code-signing
  certificate** (your IT/security team owns this), e.g. with `signtool`.
- Then distribute through your normal channel — **Intune** or **SCCM** for
  managed fleets, or an internal download page.

Signing is the single highest-impact step for a smooth rollout; everything
above works without it, but users will see warnings.

## 6. Auto-start at logon (optional)

The packaged exe supports the same logon-task install as the source build:

```
AIFluencyInsights.exe --install-startup
AIFluencyInsights.exe --uninstall-startup
```

When run from the frozen exe, the logon task points at the exe itself.

## Notes and known items

- **PyInstaller** would also produce a working exe and is faster to build, but
  it bundles Python **bytecode that can be decompiled**. Nuitka is the right
  choice when the goal is to not hand over the source.
- **No local program is impossible to reverse-engineer.** Nuitka makes it
  impractical; your internal software policy and code signing cover the rest.
- The optional `claude -p` buttons shell out to the Claude Code CLI. On the
  Windows `.cmd`-shim path this still routes text through `cmd /c`
  (Architecture doc, Finding F13 — reduced since the F5 fix blocks remote
  input, but worth closing before heavy use).
