# Contributing

## Development setup

```bash
git clone <repo-url>
cd ai-fluency-insights
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

`-e` installs in editable mode, so code changes take effect on the next run
without reinstalling. The app itself still has **no runtime dependencies**;
`[dev]` only adds pytest and ruff.

| Task | Command |
|---|---|
| Run the dashboard | `ai-fluency-insights` (or `python -m ai_fluency_insights`) |
| Run the tests | `python -m pytest` |
| Lint | `ruff check .` |
| Restart after editing (Windows) | `scripts\windows\Restart-Insights.bat` |

CI runs the same lint and tests on every pull request (Windows and Linux,
oldest and newest supported Python).

## Code layout

```
src/ai_fluency_insights/
├── cli.py, __main__.py   command line (all flags live here)
├── config.py             user-tunable settings: pricing, goals, adoption, notifications
├── paths.py              Claude Code dirs, bundled resources, per-user data dir
├── core/                 log parsing: taxonomy, pricing, Stats, parser, scanner
├── analytics/            one module per dashboard tab, pure functions over Stats
├── notifications/        thresholds, plan-usage window, toast, background watcher
├── integrations/         statusLine wizard, `claude -p`, Windows logon task
├── server/               HTTP handler, JSON API payloads, app runner
├── web/                  index.html, static/css, static/js, static/vendor (Chart.js)
└── resources/            files written out for the user (the statusLine hook script)
tests/                    mirrors the package: tests/core, tests/analytics, ...
scripts/windows/          developer convenience scripts
docs/                     user guide, metrics reference, architecture
```

### Layering rules

Imports only point **down** this list. Keep it that way; it is what lets each
part be tested, and later swapped, on its own.

1. `cli`, `server`
2. `integrations`, `notifications`
3. `analytics`
4. `core`
5. `config`, `paths`

`analytics` and `core` never do network or UI work, and nothing writes next
to the code: runtime state goes to `paths.app_data_dir()`, so the same code
runs from a checkout, a pip install, or a packaged executable.

### Where things go

- **A new metric:** compute it in the relevant `analytics/` module, register
  it in `analytics/catalog.py` with its confidence level, document the formula
  in `docs/METRICS.md`, and add a test under `tests/analytics/`.
- **A new API field or route:** `server/api.py` builds payloads; `server/handler.py`
  only routes and serialises.
- **UI changes:** `web/static/js/app.js` and `web/static/css/app.css`. No build
  step; reload the page.
- **A new setting:** `config.py`, with a comment saying what it controls.

## Commits and pull requests

- Keep each pull request to one change, with tests for new behaviour.
- `python -m pytest` and `ruff check .` must pass.
- Bump `__version__` in `src/ai_fluency_insights/__init__.py` and add a
  `CHANGELOG.md` entry for anything user-visible.
