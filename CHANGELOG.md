# Changelog

All notable changes to AI Fluency Insights. Versions follow
[Semantic Versioning](https://semver.org/).

## 1.0.0

First packaged release: a fully-local, zero-token-cost dashboard for your
Claude Code usage, delivered as an installable Python package and as a
single Windows executable that needs no source code and no Python install.

### Dashboard
- Overview, Time & Focus, Tools & Models, Projects and a Weekly Report that
  drafts your standup update.
- My Adoption scorecard (Habit, Range, Craft, Impact; six levels) and the
  AI Fluency report (Delegation, Description, Discernment, Diligence).
- Engineering-insight tabs — Flow, Quality, Value, Advisor, Data Health —
  with every number badged Observed / Derived / Proxy / Estimated.
- 100% local: reads `~/.claude/projects/` only, binds to `127.0.0.1`, zero
  token cost except two explicit `claude -p` buttons.

### Packaging and configuration
- Modular `src/` package (`core`, `analytics`, `notifications`,
  `integrations`, `server`, `web`); see [CONTRIBUTING.md](CONTRIBUTING.md).
- Installable with `pip install .`; run as `ai-fluency-insights`.
- Build a single Windows `.exe` with Nuitka — see
  [docs/PACKAGING.md](docs/PACKAGING.md).
- Optional per-user `settings.json` to override pricing, goals and
  notification thresholds without editing code — see
  [SETUP.md](SETUP.md#custom-settings).
- Runtime state is kept in the per-user data directory, so nothing is
  written next to the program.

### Security
- The local server serves only loopback requests: a Host or Origin header
  naming anything other than `localhost`/`127.0.0.1` is refused, so no other
  web page can read your stats or trigger the `claude -p` routes.
- Static files are served only from the bundled `web/static` directory;
  path traversal returns 404.

### Quality
- 160 unit tests under `tests/`; GitHub Actions CI on Windows and Linux
  (Python 3.8 and 3.13).
