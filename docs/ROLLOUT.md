# Rollout guide (for IT / packaging team)

AI Fluency Insights is a **100% local** desktop utility for developers who use
Claude Code. It reads the session logs Claude Code already writes under the
user's own `%USERPROFILE%\.claude\projects\` and serves a dashboard on
`http://localhost:8787`. It makes **no outbound network calls**, stores nothing
centrally, and needs no account or server.

This guide covers signing and distributing the prebuilt executable to a wide
internal audience. Building the executable is covered in
[PACKAGING.md](PACKAGING.md).

## What you receive from the build

`AIFluencyInsights.zip`, containing:

| File | Purpose |
|---|---|
| `AIFluencyInsights.exe` | The whole application, one self-contained file. No Python or dependencies required on the user's machine. |
| `README.txt` | One-page user instructions. |
| `settings.example.json` | Optional per-user overrides (pricing, goals). |

## 1. Code signing (do this first)

An unsigned executable triggers SmartScreen ("Windows protected your PC") and
is often blocked on managed devices. Sign it with the organization's
Authenticode code-signing certificate before distribution.

Typical `signtool` invocation (adjust cert store / timestamp URL to your PKI):

```
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a AIFluencyInsights.exe
```

Verify:

```
signtool verify /pa /v AIFluencyInsights.exe
```

Re-zip after signing so the distributed archive contains the signed binary.

## 2. Distribution

Any of these work; pick what your org already uses:

- **Intune** – package the signed `.exe` (or a wrapped `.intunewin`) as a Win32
  app. No install is strictly required (it runs standalone), so you can deploy
  it to a per-user folder and create a Start-menu shortcut, or publish the zip
  to Company Portal.
- **SCCM / MECM** – deploy as an application or package to the target
  collection.
- **Internal download page / file share** – host `AIFluencyInsights.zip`; users
  unzip and run. Simplest option; relies on signing to avoid warnings.

### Detection / "is it installed"
There is no installer and no registry footprint by default. If your tooling
needs a detection rule, deploy the `.exe` to a known path (e.g.
`%LOCALAPPDATA%\Programs\AIFluencyInsights\AIFluencyInsights.exe`) and detect
that file + version.

## 3. What it does and does not touch

| Resource | Access |
|---|---|
| `%USERPROFILE%\.claude\projects\**` | **Read only** (the user's own logs). |
| `%USERPROFILE%\.claude\settings.json` | Written **only** if the user runs `--setup-notifications`; backed up first, only the `statusLine` key changed. |
| `%LOCALAPPDATA%\AIFluencyInsights\` | Small runtime state and optional `settings.json`. |
| Network | Binds `127.0.0.1:8787` (loopback only). No outbound calls. Non-loopback / cross-origin HTTP requests are rejected. |
| Admin rights | Not required to run. `--install-startup` uses per-user Task Scheduler (no elevation). |

Port 8787 is used; if it clashes, users can run with `--port <n>`.

## 4. Optional features that use tokens

The dashboard is zero-cost except two buttons in the Weekly Report tab ("AI
summary" / "reflect") that the user may press. These shell out to the Claude
Code CLI (`claude -p`) on the user's machine, under the user's own Claude
account. They do nothing if the Claude CLI isn't installed, and they never run
on their own.

## 5. Uninstall

Delete the executable (and the shortcut, if you created one). To remove
optional extras a user may have enabled:

```
AIFluencyInsights.exe --uninstall-startup
```

and delete `%LOCALAPPDATA%\AIFluencyInsights\`.

## 6. Support notes

- **"Log directory not found"** – the user hasn't used Claude Code on that
  machine, or logs are elsewhere; run with `--dir <path>`.
- **Dashboard empty / few metrics** – not enough history yet; some tabs need a
  few active days. The in-app **Data Health** tab explains what each machine's
  logs can and cannot measure.
- **SmartScreen warning** – means the running copy is unsigned; ensure the
  signed build was the one distributed.
