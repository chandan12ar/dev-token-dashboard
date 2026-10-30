# Changelog

All notable changes to AI Fluency Insights. Versions follow
[Semantic Versioning](https://semver.org/).

## 2.0.0 — 2026-10-30

The single 5,500-line script is now an installable Python package with a
modular layout. **The dashboard itself is unchanged**: every API response was
verified byte-for-byte against 1.4 on the same logs, and all 144 tests pass.

### Changed
- Code split into `core`, `analytics`, `notifications`, `integrations`,
  `server` and `web` (see [CONTRIBUTING.md](CONTRIBUTING.md#code-layout)).
- The web UI is now real files (`index.html`, `app.css`, `app.js`) instead of
  a string inside Python, served from `/static/`.
- Run with `ai-fluency-insights` or `python -m ai_fluency_insights` after
  `pip install .`; `python ai_fluency_insights.py` no longer exists.
- Notification state moved from `.notify_state.json` next to the script to the
  per-user data directory (`%LOCALAPPDATA%\AIFluencyInsights` on Windows).
  An existing file at the repo root is carried over automatically.
- Restart scripts moved to `scripts/windows/`.
- Tests split per area under `tests/`; run with `python -m pytest`.

### Added
- `pyproject.toml` (packaging, pytest and ruff config), `--version` flag,
  GitHub Actions CI on Windows and Linux, `CONTRIBUTING.md`,
  `THIRD_PARTY_NOTICES.md`.
- Static files are served only from inside the package's `web/static`
  directory; path traversal returns 404.

### Upgrading from 1.x
1. `pip install .` from the repository root.
2. Windows auto-start users: re-run `ai-fluency-insights --install-startup`
   (the old task points at the deleted script).
3. Nothing else: the statusLine hook and your settings are unaffected.

## 1.4 and earlier

See "What's new" in the [README](README.md) for the v1.1 – v1.4 releases.
