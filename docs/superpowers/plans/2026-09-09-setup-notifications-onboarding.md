# Setup/Onboarding Path for Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--setup-notifications` CLI flag to `dev_token_dashboard.py` that safely wires Claude Code's `statusLine` hook so new users get real Anthropic plan-usage numbers without hand-editing config, plus a startup nudge for users who never run it, plus a fix for the dead `NOTIFY["enabled"]` flag.

**Architecture:** All new logic lives in `dev_token_dashboard.py` itself (single-file project — no new production files at rest, only files the wizard writes at runtime: `~/.claude/dev_token_dashboard_statusline.js` and a `settings.json` backup). A small set of pure/near-pure functions (classify existing config, compute the merged config, write it safely) are unit tested directly; a thin orchestrator wires them together behind the new CLI flag and is tested with a fake input/print and a tempdir standing in for `~/.claude`.

**Tech Stack:** Python 3.8+ standard library only (`json`, `os`, `re`, `shutil`, `sys`, `time`) — no new dependencies. The bundled statusline script is Node.js (matches Claude Code's own `statusLine` convention and the hand-written script already in use).

**Spec:** `docs/superpowers/specs/2026-09-09-setup-notifications-onboarding-design.md`

## Global Constraints

- Never write to `~/.claude/statusline.js` or any path the user chose themselves — the wizard only ever creates `~/.claude/dev_token_dashboard_statusline.js` (constant `STATUSLINE_MARKER`).
- Never modify `settings.json` when an existing `statusLine` is "foreign" (not ours) — diagnose and print guidance only.
- Always back up `settings.json` before any write to it, and write both it and the statusline script atomically (temp file + `os.replace`), matching the existing pattern in `save_notify_state`.
- Toast notifications stay Windows-only in this plan; macOS/Linux get the rate-limit-capture setup only, with a printed note. (Spec non-goal — do not add `osascript`/`notify-send` support.)
- No persisted user-preferences file — `NOTIFY` stays a source-level constant. (Spec non-goal — Approach 3 deferred.)
- A malformed (invalid JSON) existing `settings.json` must abort with an error and write nothing — never guess or overwrite a broken config.
- `--setup-notifications` must be runnable non-interactively via `--dry-run` (no prompts, no writes) for safe testing before it ever touches a real machine's config.

---

## Task 1: Classify and merge the `statusLine` config (pure functions)

**Files:**
- Modify: `dev_token_dashboard.py` — add new functions just above `def default_root():` (currently line 2910)
- Test: `test_dev_token_dashboard.py` — new `StatuslineConfigTests` class

**Interfaces:**
- Produces:
  - `STATUSLINE_MARKER = "dev_token_dashboard_statusline.js"` (module constant)
  - `claude_config_dir() -> str` — `$CLAUDE_CONFIG_DIR` if set, else `~/.claude` (same base `default_root()` uses, without the `/projects` suffix)
  - `load_settings_json(path: str) -> tuple[dict | None, str | None]` — `(settings, None)` on success (a missing file returns `({}, None)`); `(None, error_message)` if the file exists but isn't valid JSON or can't be read
  - `classify_statusline(settings: dict) -> str` — one of `"missing"`, `"ours"`, `"foreign"`
  - `merge_statusline_config(settings: dict, statusline_js_path: str) -> dict` — returns a **new** dict with `statusLine` added (if `"missing"`) or patched to `refreshInterval: 30` (if `"ours"`); only ever called when `classify_statusline(settings)` is `"missing"` or `"ours"` — behavior for `"foreign"` input is undefined and not this function's job (Task 4 handles that case separately)

- [ ] **Step 1: Write the failing tests**

Add to `test_dev_token_dashboard.py`:

```python
class StatuslineConfigTests(unittest.TestCase):
    def test_classify_missing_when_no_statusline_key(self):
        self.assertEqual(dtd.classify_statusline({}), "missing")

    def test_classify_missing_when_statusline_not_a_dict(self):
        self.assertEqual(dtd.classify_statusline({"statusLine": "oops"}), "missing")

    def test_classify_missing_when_command_absent(self):
        self.assertEqual(dtd.classify_statusline({"statusLine": {"type": "command"}}), "missing")

    def test_classify_ours_when_marker_in_command(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "C:\\Users\\x\\.claude\\dev_token_dashboard_statusline.js"'}}
        self.assertEqual(dtd.classify_statusline(settings), "ours")

    def test_classify_foreign_when_other_command(self):
        settings = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
        self.assertEqual(dtd.classify_statusline(settings), "foreign")

    def test_merge_adds_full_block_when_missing(self):
        new = dtd.merge_statusline_config({}, "/home/x/.claude/dev_token_dashboard_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command",
            "command": 'node "/home/x/.claude/dev_token_dashboard_statusline.js"',
            "refreshInterval": 30,
        })

    def test_merge_preserves_other_top_level_keys(self):
        settings = {"model": "opusplan", "enabledPlugins": {"foo": True}}
        new = dtd.merge_statusline_config(settings, "/x/dev_token_dashboard_statusline.js")
        self.assertEqual(new["model"], "opusplan")
        self.assertEqual(new["enabledPlugins"], {"foo": True})

    def test_merge_patches_refresh_interval_only_when_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'},
                    "model": "opusplan"}
        new = dtd.merge_statusline_config(settings, "/x/dev_token_dashboard_statusline.js")
        self.assertEqual(new["statusLine"]["refreshInterval"], 30)
        self.assertEqual(new["statusLine"]["command"], settings["statusLine"]["command"])
        self.assertEqual(new["model"], "opusplan")

    def test_merge_is_a_noop_when_already_correct(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"',
                                    "refreshInterval": 30}}
        new = dtd.merge_statusline_config(settings, "/x/dev_token_dashboard_statusline.js")
        self.assertEqual(new, settings)


class LoadSettingsJsonTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict_no_error(self):
        settings, err = dtd.load_settings_json("/no/such/settings.json")
        self.assertEqual(settings, {})
        self.assertIsNone(err)

    def test_valid_json_returns_parsed_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"model": "opusplan"}')
            settings, err = dtd.load_settings_json(path)
            self.assertEqual(settings, {"model": "opusplan"})
            self.assertIsNone(err)

    def test_malformed_json_returns_error_not_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            settings, err = dtd.load_settings_json(path)
            self.assertIsNone(settings)
            self.assertIsNotNone(err)


class ClaudeConfigDirTests(unittest.TestCase):
    def test_uses_env_var_when_set(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/custom/dir"}):
            self.assertEqual(dtd.claude_config_dir(), "/custom/dir")

    def test_falls_back_to_home_claude(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            expected = os.path.join(os.path.expanduser("~"), ".claude")
            self.assertEqual(dtd.claude_config_dir(), expected)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest test_dev_token_dashboard.StatuslineConfigTests test_dev_token_dashboard.LoadSettingsJsonTests test_dev_token_dashboard.ClaudeConfigDirTests -v`
Expected: FAIL with `AttributeError: module 'dev_token_dashboard' has no attribute 'classify_statusline'` (and similarly for the other new names).

- [ ] **Step 3: Implement the functions**

Insert into `dev_token_dashboard.py` immediately above `def default_root():`:

```python
# ---------------------------------------------------------------------------
# Setup wizard (--setup-notifications): wires the statusLine hook so plan-
# usage % and toast milestones use real Anthropic numbers instead of the
# local estimate. See docs/superpowers/specs/2026-09-09-setup-notifications-
# onboarding-design.md for the full design.
# ---------------------------------------------------------------------------

STATUSLINE_MARKER = "dev_token_dashboard_statusline.js"


def claude_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def load_settings_json(path):
    """Like load_notify_state, but distinguishes a missing file (fine, an
    empty settings.json is valid) from a malformed one (must abort rather
    than silently treat a broken config as empty and overwrite it)."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON: {e}"
    except OSError as e:
        return None, f"could not read {path}: {e}"


def classify_statusline(settings):
    sl = settings.get("statusLine")
    if not isinstance(sl, dict) or not sl.get("command"):
        return "missing"
    if STATUSLINE_MARKER in sl["command"]:
        return "ours"
    return "foreign"


def merge_statusline_config(settings, statusline_js_path):
    new_settings = dict(settings)
    if classify_statusline(settings) == "missing":
        new_settings["statusLine"] = {
            "type": "command",
            "command": f'node "{statusline_js_path}"',
            "refreshInterval": 30,
        }
    else:  # "ours" -- caller must not call this for "foreign"
        new_settings["statusLine"] = dict(settings["statusLine"], refreshInterval=30)
    return new_settings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_dev_token_dashboard.StatuslineConfigTests test_dev_token_dashboard.LoadSettingsJsonTests test_dev_token_dashboard.ClaudeConfigDirTests -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add dev_token_dashboard.py test_dev_token_dashboard.py
git commit -m "Add statusLine config classify/merge helpers for setup wizard"
```

---

## Task 2: Bundled statusline script, node check, and safe settings write

**Files:**
- Modify: `dev_token_dashboard.py` — add `import shutil` to the top imports; add new functions/constant after Task 1's block (still above `def default_root():`)
- Test: `test_dev_token_dashboard.py` — new `WriteSettingsWithBackupTests` and `NodeAvailableTests` classes

**Interfaces:**
- Consumes: none from Task 1 (independent additions), but lives in the same file section
- Produces:
  - `BUNDLED_STATUSLINE_JS: str` — full contents of the script the wizard writes
  - `node_available() -> bool`
  - `write_settings_with_backup(settings_path: str, new_settings: dict) -> str | None` — writes `new_settings` as pretty JSON to `settings_path` atomically; if a file already existed at `settings_path`, first copies its exact prior bytes to `f"{settings_path}.bak-{int(time.time())}"` and returns that backup path; returns `None` if there was nothing to back up

- [ ] **Step 1: Write the failing tests**

```python
class NodeAvailableTests(unittest.TestCase):
    def test_true_when_which_finds_node(self):
        with patch.object(dtd.shutil, "which", return_value=r"C:\nodejs\node.exe"):
            self.assertTrue(dtd.node_available())

    def test_false_when_which_finds_nothing(self):
        with patch.object(dtd.shutil, "which", return_value=None):
            self.assertFalse(dtd.node_available())


class WriteSettingsWithBackupTests(unittest.TestCase):
    def test_no_backup_when_file_did_not_exist(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            backup = dtd.write_settings_with_backup(path, {"a": 1})
            self.assertIsNone(backup)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 1})

    def test_backs_up_exact_prior_content_then_writes_new(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"old": true}')
            backup = dtd.write_settings_with_backup(path, {"new": True})
            self.assertIsNotNone(backup)
            self.assertTrue(os.path.exists(backup))
            with open(backup, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"old": true}')
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"new": True})

    def test_written_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            dtd.write_settings_with_backup(path, {"statusLine": {"type": "command"}})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"statusLine": {"type": "command"}})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest test_dev_token_dashboard.NodeAvailableTests test_dev_token_dashboard.WriteSettingsWithBackupTests -v`
Expected: FAIL — `dtd.shutil` and `dtd.node_available`/`dtd.write_settings_with_backup` don't exist yet.

- [ ] **Step 3: Implement**

Add `import shutil` next to the other stdlib imports at the top of `dev_token_dashboard.py` (alphabetical, next to `import re`).

Add to the setup-wizard section (after Task 1's functions):

```python
def node_available():
    return shutil.which("node") is not None


def write_settings_with_backup(settings_path, new_settings):
    backup_path = None
    if os.path.exists(settings_path):
        backup_path = f"{settings_path}.bak-{int(time.time())}"
        with open(settings_path, "r", encoding="utf-8") as f:
            original = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(original)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new_settings, f, indent=2)
    os.replace(tmp, settings_path)
    return backup_path


# Written verbatim to <claude_config_dir>/dev_token_dashboard_statusline.js by
# run_setup_notifications() -- never to statusline.js or any user-chosen path.
# Trimmed from the hand-written ~/.claude/statusline.js already in use on the
# maintainer's machine: same merge-onto-previous-snapshot side effect, plus a
# minimal one-line status output for users who had no statusLine at all.
BUNDLED_STATUSLINE_JS = r"""// dev-token-dashboard:managed-statusline
// Captures Anthropic's real rate_limits (five_hour/seven_day) for
// dev_token_dashboard.py's plan-usage panel and toast notifications.
// See docs/DASHBOARD_GUIDE.md#plan-usage-limits.
const fs = require('fs');
const os = require('os');
const path = require('path');

let raw = '';
process.stdin.on('data', d => raw += d);
process.stdin.on('end', () => {
  let j = {};
  try { j = JSON.parse(raw); } catch (e) { /* fall through with j={} */ }

  if (j.rate_limits) {
    const outPath = path.join(os.homedir(), '.claude', 'rate_limits_latest.json');
    let merged = {};
    try {
      const prev = JSON.parse(fs.readFileSync(outPath, 'utf8'));
      merged = Object.assign({}, prev.rate_limits);
    } catch (e) { /* no previous snapshot, or unreadable -- start fresh */ }
    Object.assign(merged, j.rate_limits);
    const payload = JSON.stringify({
      rate_limits: merged,
      captured_at: Date.now() / 1000,
    });
    try {
      const tmp = outPath + '.tmp';
      fs.writeFileSync(tmp, payload);
      fs.renameSync(tmp, outPath);
    } catch (e) { /* best-effort; never break the status line over this */ }
  }

  const dir = (j.workspace && j.workspace.current_dir) || j.cwd || '';
  const model = (j.model && j.model.display_name) || '';
  const ctx = j.context_window && j.context_window.used_percentage;
  const rl = j.rate_limits || {};
  const five = rl.five_hour && rl.five_hour.used_percentage;
  const seven = rl.seven_day && rl.seven_day.used_percentage;

  let line = dir + '  ' + model;
  if (ctx != null) line += `  ctx ${Math.round(ctx)}%`;
  if (five != null) line += `  5h ${Math.round(five)}%`;
  if (seven != null) line += `  wk ${Math.round(seven)}%`;
  process.stdout.write(line);
});
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_dev_token_dashboard.NodeAvailableTests test_dev_token_dashboard.WriteSettingsWithBackupTests -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite to check nothing else broke**

Run: `python -m unittest test_dev_token_dashboard -v`
Expected: all PASS (previous count + new tests).

- [ ] **Step 6: Commit**

```bash
git add dev_token_dashboard.py test_dev_token_dashboard.py
git commit -m "Add bundled statusline script, node check, and safe settings writer"
```

---

## Task 3: `run_setup_notifications` orchestrator — missing/ours/dry-run

**Files:**
- Modify: `dev_token_dashboard.py` — add after Task 2's block, still above `def default_root():`
- Test: `test_dev_token_dashboard.py` — new `RunSetupNotificationsTests` class

**Interfaces:**
- Consumes: `claude_config_dir`, `load_settings_json`, `classify_statusline`, `merge_statusline_config`, `node_available`, `write_settings_with_backup`, `BUNDLED_STATUSLINE_JS` (Tasks 1-2); `load_rate_limits`, `RATE_LIMITS_PATH`, `NOTIFY` (existing)
- Produces: `run_setup_notifications(claude_dir: str | None = None, dry_run: bool = False, input_fn=input, print_fn=print, isatty_fn=None) -> int` — returns a process exit code (0 success/cancelled, 1 on error). `isatty_fn` defaults to `sys.stdin.isatty` and exists purely so tests can force the "not interactive" branch without faking stdin.
- This task implements the `"missing"` and `"ours"` branches only. Task 4 adds `"foreign"`.

- [ ] **Step 1: Write the failing tests**

```python
class RunSetupNotificationsTests(unittest.TestCase):
    def _run(self, claude_dir, **kwargs):
        prints = []
        kwargs.setdefault("print_fn", prints.append)
        kwargs.setdefault("isatty_fn", lambda: True)
        code = dtd.run_setup_notifications(claude_dir=claude_dir, **kwargs)
        return code, prints

    @patch.object(dtd, "node_available", return_value=True)
    def test_dry_run_writes_nothing(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, dry_run=True, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertFalse(os.path.exists(os.path.join(d, dtd.STATUSLINE_MARKER)))
            self.assertTrue(any("Dry run" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_missing_statusline_writes_after_yes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 0)
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, dtd.STATUSLINE_MARKER)
            self.assertTrue(os.path.exists(settings_path))
            self.assertTrue(os.path.exists(js_path))
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
            self.assertIn(dtd.STATUSLINE_MARKER, settings["statusLine"]["command"])
            self.assertEqual(settings["statusLine"]["refreshInterval"], 30)

    @patch.object(dtd, "node_available", return_value=True)
    def test_missing_statusline_declines_on_no(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "n")
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("Cancelled" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_ours_already_correct_reports_no_changes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, dtd.STATUSLINE_MARKER)
            existing = {"statusLine": {"type": "command",
                                        "command": f'node "{js_path}"', "refreshInterval": 30}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(existing, f)
            code, prints = self._run(d, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertTrue(any("Already configured" in p for p in prints))
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), existing)  # untouched

    @patch.object(dtd, "node_available", return_value=False)
    def test_missing_node_aborts_without_writing(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("node" in p.lower() for p in prints))

    def test_malformed_settings_aborts_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                f.write("{not json")
            code, prints = self._run(d, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertTrue(any("not valid JSON" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_non_interactive_without_dry_run_aborts(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, isatty_fn=lambda: False,
                                      input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("not running interactively" in p.lower() for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_final_report_shown_after_successful_write(self, _node):
        with tempfile.TemporaryDirectory() as d:
            _, prints = self._run(d, input_fn=lambda _: "y")
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))
            self.assertTrue(any("Toast notifications:" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    @patch.object(dtd, "write_settings_with_backup", side_effect=OSError("Permission denied"))
    def test_write_permission_error_reported_not_crashed(self, _write, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 1)
            self.assertTrue(any("permission denied" in p.lower() for p in prints))
            # the statusline script write happens before the settings write in
            # source order, so it may exist -- but settings.json must not, since
            # write_settings_with_backup is what raised before touching it
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest test_dev_token_dashboard.RunSetupNotificationsTests -v`
Expected: FAIL — `dtd.run_setup_notifications` doesn't exist yet.

- [ ] **Step 3: Implement**

Add to the setup-wizard section:

```python
def _print_final_report(print_fn):
    fresh = load_rate_limits(RATE_LIMITS_PATH) is not None
    print_fn("")
    print_fn("Rate-limit capture:  " + ("ok, fresh" if fresh else
              "not yet captured (will appear after your next Claude Code message)"))
    print_fn("Toast notifications: " + ("ON" if NOTIFY.get("enabled", True) else "OFF") +
              '  (edit NOTIFY["enabled"] in the script to change)')


def run_setup_notifications(claude_dir=None, dry_run=False, input_fn=input,
                             print_fn=print, isatty_fn=None):
    isatty_fn = isatty_fn or sys.stdin.isatty
    claude_dir = claude_dir or claude_config_dir()
    settings_path = os.path.join(claude_dir, "settings.json")
    statusline_js_path = os.path.join(claude_dir, STATUSLINE_MARKER)

    if os.name != "nt":
        print_fn("Toast notifications aren't available on this OS yet -- "
                  "setting up rate-limit capture only.")

    settings, err = load_settings_json(settings_path)
    if err:
        print_fn(f"[!] {err}")
        print_fn("    Fix or remove this file, then re-run --setup-notifications.")
        return 1

    kind = classify_statusline(settings)

    if kind == "foreign":
        _report_foreign_statusline(settings, print_fn)
        _print_final_report(print_fn)
        return 0

    if not node_available():
        print_fn("[!] `node` was not found on PATH. Claude Code itself requires "
                  "Node.js, so this is unexpected -- install it and re-run.")
        return 1

    new_settings = merge_statusline_config(settings, statusline_js_path)
    if new_settings == settings:
        print_fn(f"Already configured -- {settings_path} needs no changes.")
        _print_final_report(print_fn)
        return 0

    print_fn("This will write:")
    print_fn(f"  {statusline_js_path}")
    print_fn(f"  statusLine block in {settings_path}:")
    print_fn(json.dumps(new_settings["statusLine"], indent=2))

    if dry_run:
        print_fn("Dry run -- nothing written.")
        return 0

    if not isatty_fn():
        print_fn("[!] Not running interactively -- re-run from a terminal, "
                  "or pass --dry-run to preview only.")
        return 1

    answer = input_fn("Proceed? [y/N] ").strip().lower()
    if answer != "y":
        print_fn("Cancelled -- nothing written.")
        return 0

    with open(statusline_js_path, "w", encoding="utf-8") as f:
        f.write(BUNDLED_STATUSLINE_JS)
    try:
        backup_path = write_settings_with_backup(settings_path, new_settings)
    except OSError as e:
        print_fn(f"[!] Could not write {settings_path}: {e}")
        print_fn(f"    {statusline_js_path} was written, but the statusLine hook "
                  "isn't wired in yet -- fix the permissions and re-run.")
        return 1
    if backup_path:
        print_fn(f"Backed up previous settings to {backup_path}")
    print_fn(f"Wrote {statusline_js_path} and updated {settings_path}.")
    _print_final_report(print_fn)
    return 0
```

Add a temporary stub for the not-yet-built Task 4 dependency so this task's tests (which don't exercise the "foreign" path) can still import cleanly — Task 4 replaces this stub with the real implementation:

```python
def _report_foreign_statusline(settings, print_fn):
    raise NotImplementedError("implemented in Task 4")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_dev_token_dashboard.RunSetupNotificationsTests -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_dev_token_dashboard -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add dev_token_dashboard.py test_dev_token_dashboard.py
git commit -m "Add run_setup_notifications orchestrator for missing/ours/dry-run"
```

---

## Task 4: `run_setup_notifications` — foreign statusLine diagnostics

**Files:**
- Modify: `dev_token_dashboard.py` — replace the `_report_foreign_statusline` stub from Task 3
- Test: `test_dev_token_dashboard.py` — new `ForeignStatuslineTests` class

**Interfaces:**
- Consumes: `load_rate_limits`, `RATE_LIMITS_PATH` (existing); `run_setup_notifications` (Task 3, unchanged signature)
- Produces: `_report_foreign_statusline(settings: dict, print_fn) -> None` (real implementation) and, for testability, `_extract_script_path(command: str) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
class ForeignStatuslineTests(unittest.TestCase):
    def test_extract_script_path_from_quoted_command(self):
        self.assertEqual(
            dtd._extract_script_path('node "C:\\Users\\x\\.claude\\statusline.js"'),
            "C:\\Users\\x\\.claude\\statusline.js")

    def test_extract_script_path_returns_none_when_unquoted(self):
        self.assertIsNone(dtd._extract_script_path("~/.claude/statusline.sh"))

    @patch.object(dtd, "node_available", return_value=True)
    def test_foreign_statusline_does_not_write_settings(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            original = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(original, f)
            prints = []
            code = dtd.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                input_fn=lambda _: self.fail("must not prompt"),
                                                isatty_fn=lambda: True)
            self.assertEqual(code, 0)
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), original)
            self.assertFalse(os.path.exists(os.path.join(d, dtd.STATUSLINE_MARKER)))
            self.assertTrue(any("existing statusLine" in p.lower() for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_foreign_statusline_reports_fresh_capture_if_present(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}, f)
            rl_path = os.path.join(d, "rate_limits_latest.json")
            with open(rl_path, "w", encoding="utf-8") as f:
                json.dump({"rate_limits": {}, "captured_at": dtd.time.time()}, f)
            prints = []
            with patch.object(dtd, "RATE_LIMITS_PATH", rl_path):
                dtd.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                             input_fn=lambda _: self.fail("must not prompt"),
                                             isatty_fn=lambda: True)
            self.assertTrue(any("already has a fresh capture" in p for p in prints))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest test_dev_token_dashboard.ForeignStatuslineTests -v`
Expected: FAIL — `_extract_script_path` doesn't exist, and the stub `_report_foreign_statusline` raises `NotImplementedError`.

- [ ] **Step 3: Implement**

Replace the Task 3 stub with:

```python
def _extract_script_path(command):
    m = re.search(r'"([^"]+)"', command)
    return m.group(1) if m else None


def _report_foreign_statusline(settings, print_fn):
    cmd = settings["statusLine"]["command"]
    print_fn(f"An existing statusLine is already configured: {cmd}")
    script_path = _extract_script_path(cmd)
    if script_path and os.path.isfile(script_path):
        try:
            with open(script_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "rate_limits" in content:
                print_fn("Your existing script already looks like it references rate_limits.")
        except OSError:
            pass
    if load_rate_limits(RATE_LIMITS_PATH) is not None:
        print_fn(f"{RATE_LIMITS_PATH} already has a fresh capture -- you may already be covered.")
    else:
        print_fn(f"{RATE_LIMITS_PATH} has no fresh capture yet.")
    print_fn("To add rate-limit capture to your own script, make it write this JSON")
    print_fn("shape to ~/.claude/rate_limits_latest.json whenever `rate_limits` is")
    print_fn("present in its stdin input (see the reference script below):")
    print_fn(BUNDLED_STATUSLINE_JS)
    print_fn("Full explanation: docs/DASHBOARD_GUIDE.md#plan-usage-limits")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_dev_token_dashboard.ForeignStatuslineTests -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_dev_token_dashboard -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add dev_token_dashboard.py test_dev_token_dashboard.py
git commit -m "Add foreign-statusLine diagnostics to setup wizard"
```

---

## Task 5: Wire `--setup-notifications`/`--dry-run` into the CLI

**Files:**
- Modify: `dev_token_dashboard.py:main()` (currently starting at line 2917)

**Interfaces:**
- Consumes: `run_setup_notifications` (Task 3/4)
- Produces: none new — this is the CLI surface only

No new automated test — `main()`'s existing CLI actions (`--install-startup` etc.) aren't unit tested either; verified manually in Step 3 below, matching that precedent. The underlying logic is already fully covered by Tasks 1-4's tests.

- [ ] **Step 1: Add the flags and dispatch**

In `main()`, find:

```python
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="Windows: remove the logon task")
    args = ap.parse_args()

    if args.install_startup or args.uninstall_startup:
```

Change to:

```python
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="Windows: remove the logon task")
    ap.add_argument("--setup-notifications", action="store_true",
                    help="Wire up the statusLine hook for accurate plan-usage %% "
                         "and toast notifications (Windows)")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --setup-notifications, show what would change "
                         "without writing anything")
    args = ap.parse_args()

    if args.setup_notifications:
        sys.exit(run_setup_notifications(dry_run=args.dry_run))

    if args.install_startup or args.uninstall_startup:
```

- [ ] **Step 2: Manually verify the dry-run path**

Run: `python dev_token_dashboard.py --setup-notifications --dry-run`
Expected: prints what it would write (or "Already configured" if this machine already has the marker-tagged statusLine from a previous run), makes no file changes. Confirm with `git status` / checking `~/.claude/settings.json` mtime that nothing changed.

- [ ] **Step 3: Manually verify against a scratch config, not the real one**

```bash
scratch=$(mktemp -d)
CLAUDE_CONFIG_DIR="$scratch" python dev_token_dashboard.py --setup-notifications
```
Answer `y` at the prompt. Expected: `$scratch/settings.json` and
`$scratch/dev_token_dashboard_statusline.js` are created; the real
`~/.claude/settings.json` is untouched. Re-run the same command (same
`$scratch`) — expected: "Already configured -- ... needs no changes."
Clean up afterward: `rm -rf "$scratch"`.

- [ ] **Step 4: Run the full test suite**

Run: `python -m unittest test_dev_token_dashboard -v`
Expected: all PASS (nothing in this task touches tested logic, this just confirms the file still imports and runs cleanly).

- [ ] **Step 5: Commit**

```bash
git add dev_token_dashboard.py
git commit -m "Wire --setup-notifications and --dry-run into the CLI"
```

---

## Task 6: Fix the dead `NOTIFY["enabled"]` flag

**Files:**
- Modify: `dev_token_dashboard.py` — add helper near `NOTIFY` (line 68); modify `main()`'s thread-start (currently line 2984)
- Test: `test_dev_token_dashboard.py` — new `ShouldRunNotificationsTests` class

**Interfaces:**
- Produces: `should_run_notifications(notify: dict | None = None) -> bool` — reads `notify["enabled"]` (defaulting to the module-level `NOTIFY` dict, and to `True` if the key itself is absent)

- [ ] **Step 1: Write the failing tests**

```python
class ShouldRunNotificationsTests(unittest.TestCase):
    def test_true_by_default(self):
        self.assertTrue(dtd.should_run_notifications(dict(dtd.NOTIFY, enabled=True)))

    def test_false_when_disabled(self):
        self.assertFalse(dtd.should_run_notifications(dict(dtd.NOTIFY, enabled=False)))

    def test_true_when_key_absent(self):
        notify = dict(dtd.NOTIFY)
        del notify["enabled"]
        self.assertTrue(dtd.should_run_notifications(notify))

    def test_defaults_to_module_level_notify(self):
        with patch.dict(dtd.NOTIFY, {"enabled": False}):
            self.assertFalse(dtd.should_run_notifications())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest test_dev_token_dashboard.ShouldRunNotificationsTests -v`
Expected: FAIL — `dtd.should_run_notifications` doesn't exist.

- [ ] **Step 3: Implement**

Just below the `NOTIFY = { ... }` dict definition (around line 90, after `OFFICIAL_MILESTONES`), add:

```python
def should_run_notifications(notify=None):
    notify = NOTIFY if notify is None else notify
    return bool(notify.get("enabled", True))
```

In `main()`, find:

```python
    threading.Thread(target=_notify_loop, daemon=True).start()
    try:
        srv.serve_forever()
```

Change to:

```python
    if should_run_notifications():
        threading.Thread(target=_notify_loop, daemon=True).start()
    else:
        print('  Toast notifications: OFF (NOTIFY["enabled"] is False)')
    try:
        srv.serve_forever()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_dev_token_dashboard.ShouldRunNotificationsTests -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_dev_token_dashboard -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add dev_token_dashboard.py test_dev_token_dashboard.py
git commit -m "Fix NOTIFY[\"enabled\"] so it actually gates the notification thread"
```

---

## Task 7: Startup nudge when rate-limit capture has never run

**Files:**
- Modify: `dev_token_dashboard.py` — add helper near Task 1's block; modify `main()` (around the existing `print(f"  Reading logs from: {args.dir}")` line, currently 2971)
- Test: `test_dev_token_dashboard.py` — new `StartupHintTests` class

**Interfaces:**
- Consumes: `RATE_LIMITS_PATH` (existing)
- Produces: `startup_hint(rate_limits_path: str | None = None, is_windows: bool | None = None) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
class StartupHintTests(unittest.TestCase):
    def test_none_on_non_windows_even_if_missing(self):
        self.assertIsNone(dtd.startup_hint("/no/such/path.json", is_windows=False))

    def test_hint_when_windows_and_never_captured(self):
        hint = dtd.startup_hint("/no/such/path.json", is_windows=True)
        self.assertIsNotNone(hint)
        self.assertIn("--setup-notifications", hint)

    def test_none_when_file_already_exists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rate_limits_latest.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertIsNone(dtd.startup_hint(path, is_windows=True))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest test_dev_token_dashboard.StartupHintTests -v`
Expected: FAIL — `dtd.startup_hint` doesn't exist.

- [ ] **Step 3: Implement**

Add near the other setup-wizard helpers:

```python
def startup_hint(rate_limits_path=None, is_windows=None):
    rate_limits_path = rate_limits_path or RATE_LIMITS_PATH
    is_windows = (os.name == "nt") if is_windows is None else is_windows
    if is_windows and not os.path.exists(rate_limits_path):
        return "  Tip: plan-usage % is a local estimate. Run --setup-notifications for real Anthropic numbers."
    return None
```

In `main()`, find:

```python
    print(f"  Dev Token Dashboard running at {url}")
    print(f"  Reading logs from: {args.dir}")
    print("  Ctrl+C to stop.")
```

Change to:

```python
    print(f"  Dev Token Dashboard running at {url}")
    print(f"  Reading logs from: {args.dir}")
    hint = startup_hint()
    if hint:
        print(hint)
    print("  Ctrl+C to stop.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_dev_token_dashboard.StartupHintTests -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_dev_token_dashboard -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add dev_token_dashboard.py test_dev_token_dashboard.py
git commit -m "Add startup hint pointing to --setup-notifications when never run"
```

---

## Task 8: Documentation

**Files:**
- Modify: `SETUP.md` — add a subsection after the existing "3. Optional: start automatically at logon" section
- Modify: `docs/DASHBOARD_GUIDE.md` — extend the "Plan usage limits" section added 2026-09-09

No automated test — verify with a manual read-through and `grep` checks below.

- [ ] **Step 1: Update SETUP.md**

Insert a new `## 3b. Optional: accurate plan-usage numbers and toast notifications` section after the existing section 3 (auto-start at logon) and before `## 4. Optional: AI weekly summaries`:

```markdown
## 3b. Optional: accurate plan-usage numbers and toast notifications

By default the "Plan usage limits" card on the dashboard shows a rough
local estimate. To get Anthropic's real 5-hour/7-day numbers (and, on
Windows, toast pop-ups as you cross usage milestones), run:

```
python dev_token_dashboard.py --setup-notifications
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
```

- [ ] **Step 2: Update docs/DASHBOARD_GUIDE.md**

In the "Plan usage limits" section (added earlier today), find the paragraph starting "*How it's captured:*" and add, immediately after it:

```markdown
*Setting it up:* run `python dev_token_dashboard.py --setup-notifications`
(add `--dry-run` to preview first) — it configures the `statusLine` hook
for you and won't touch an existing custom one. See
[SETUP.md](../SETUP.md#3b-optional-accurate-plan-usage-numbers-and-toast-notifications).
```

- [ ] **Step 3: Verify**

Run: `grep -n "setup-notifications" SETUP.md docs/DASHBOARD_GUIDE.md`
Expected: at least one match in each file.

- [ ] **Step 4: Commit**

```bash
git add SETUP.md docs/DASHBOARD_GUIDE.md
git commit -m "Document --setup-notifications in SETUP.md and DASHBOARD_GUIDE.md"
```
