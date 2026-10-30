"""The --setup-notifications wizard and statusLine hook handling."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_fluency_insights.config import NOTIFY
from ai_fluency_insights.integrations import statusline as statusline_mod
from ai_fluency_insights.integrations.statusline import (
    BUNDLED_STATUSLINE_JS,
    STATUSLINE_MARKER,
    _extract_script_path,
    _print_final_report,
    _report_foreign_statusline,
    classify_statusline,
    load_settings_json,
    merge_statusline_config,
    node_available,
    run_setup_notifications,
    startup_hint,
    write_settings_with_backup,
)
from ai_fluency_insights.paths import claude_config_dir


class StatuslineConfigTests(unittest.TestCase):
    def test_classify_missing_when_no_statusline_key(self):
        self.assertEqual(classify_statusline({}), "missing")

    def test_classify_missing_when_statusline_not_a_dict(self):
        self.assertEqual(classify_statusline({"statusLine": "oops"}), "missing")

    def test_classify_missing_when_command_absent(self):
        self.assertEqual(classify_statusline({"statusLine": {"type": "command"}}), "missing")

    def test_classify_ours_when_marker_in_command(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "C:\\Users\\x\\.claude\\ai_fluency_insights_statusline.js"'}}
        self.assertEqual(classify_statusline(settings), "ours")

    def test_classify_foreign_when_other_command(self):
        settings = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
        self.assertEqual(classify_statusline(settings), "foreign")

    def test_classify_missing_when_command_not_a_string(self):
        # A non-string command (e.g. an int, from a hand-edited settings.json)
        # must not raise TypeError from `in` on a non-string. It's treated as
        # "missing" (no usable command string present), consistent with the
        # existing convention that any malformed shape -- statusLine not a
        # dict, command key absent -- also classifies as "missing" rather
        # than "foreign".
        settings = {"statusLine": {"type": "command", "command": 12345}}
        self.assertEqual(classify_statusline(settings), "missing")

    def test_merge_adds_full_block_when_missing(self):
        new = merge_statusline_config({}, "/home/x/.claude/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command",
            "command": 'node "/home/x/.claude/ai_fluency_insights_statusline.js"',
            "refreshInterval": 30,
        })

    def test_merge_preserves_other_top_level_keys(self):
        settings = {"model": "opusplan", "enabledPlugins": {"foo": True}}
        new = merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["model"], "opusplan")
        self.assertEqual(new["enabledPlugins"], {"foo": True})

    def test_merge_patches_refresh_interval_only_when_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/ai_fluency_insights_statusline.js"'},
                    "model": "opusplan"}
        new = merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"]["refreshInterval"], 30)
        self.assertEqual(new["statusLine"]["command"], settings["statusLine"]["command"])
        self.assertEqual(new["model"], "opusplan")

    def test_merge_is_a_noop_when_already_correct(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/ai_fluency_insights_statusline.js"',
                                    "refreshInterval": 30}}
        new = merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new, settings)

    def test_classify_legacy_pre_rename_install_as_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'}}
        self.assertEqual(classify_statusline(settings), "ours")

    def test_merge_migrates_legacy_install_to_new_script_path(self):
        settings = {"statusLine": {"type": "command", "padding": 2,
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'}}
        new = merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command", "padding": 2, "refreshInterval": 30,
            "command": 'node "/x/ai_fluency_insights_statusline.js"',
        })


class LoadSettingsJsonTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict_no_error(self):
        settings, err = load_settings_json("/no/such/settings.json")
        self.assertEqual(settings, {})
        self.assertIsNone(err)

    def test_valid_json_returns_parsed_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"model": "opusplan"}')
            settings, err = load_settings_json(path)
            self.assertEqual(settings, {"model": "opusplan"})
            self.assertIsNone(err)

    def test_malformed_json_returns_error_not_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            settings, err = load_settings_json(path)
            self.assertIsNone(settings)
            self.assertIsNotNone(err)


class ClaudeConfigDirTests(unittest.TestCase):
    def test_uses_env_var_when_set(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/custom/dir"}):
            self.assertEqual(claude_config_dir(), "/custom/dir")

    def test_falls_back_to_home_claude(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            expected = os.path.join(os.path.expanduser("~"), ".claude")
            self.assertEqual(claude_config_dir(), expected)


class NodeAvailableTests(unittest.TestCase):
    def test_true_when_which_finds_node(self):
        with patch.object(shutil, "which", return_value=r"C:\nodejs\node.exe"):
            self.assertTrue(node_available())

    def test_false_when_which_finds_nothing(self):
        with patch.object(shutil, "which", return_value=None):
            self.assertFalse(node_available())


class WriteSettingsWithBackupTests(unittest.TestCase):
    def test_no_backup_when_file_did_not_exist(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            backup = write_settings_with_backup(path, {"a": 1})
            self.assertIsNone(backup)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 1})

    def test_backs_up_exact_prior_content_then_writes_new(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            # Use multi-line JSON with embedded newlines to detect text-mode translation bugs
            original_content = '{\n  "old": true\n}'
            with open(path, "w", encoding="utf-8") as f:
                f.write(original_content)
            # Read the original bytes before calling the function
            with open(path, "rb") as f:
                original_bytes = f.read()
            backup = write_settings_with_backup(path, {"new": True})
            self.assertIsNotNone(backup)
            self.assertTrue(os.path.exists(backup))
            # Check backup filename format
            self.assertRegex(backup, r".*\.bak-\d+$")
            # Verify backup preserves exact bytes (binary comparison to catch newline translation)
            with open(backup, "rb") as f_backup:
                backup_bytes = f_backup.read()
            self.assertEqual(backup_bytes, original_bytes)
            # Verify new settings were written
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"new": True})

    def test_written_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            write_settings_with_backup(path, {"statusLine": {"type": "command"}})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"statusLine": {"type": "command"}})


class RunSetupNotificationsTests(unittest.TestCase):
    def _run(self, claude_dir, **kwargs):
        prints = []
        kwargs.setdefault("print_fn", prints.append)
        kwargs.setdefault("isatty_fn", lambda: True)
        code = run_setup_notifications(claude_dir=claude_dir, **kwargs)
        return code, prints

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_dry_run_writes_nothing(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, dry_run=True, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertFalse(os.path.exists(os.path.join(d, STATUSLINE_MARKER)))
            self.assertTrue(any("Dry run" in p for p in prints))

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_missing_statusline_writes_after_yes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 0)
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, STATUSLINE_MARKER)
            self.assertTrue(os.path.exists(settings_path))
            self.assertTrue(os.path.exists(js_path))
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
            self.assertIn(STATUSLINE_MARKER, settings["statusLine"]["command"])
            self.assertEqual(settings["statusLine"]["refreshInterval"], 30)

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_missing_statusline_declines_on_no(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "n")
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("Cancelled" in p for p in prints))

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_ours_already_correct_reports_no_changes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, STATUSLINE_MARKER)
            existing = {"statusLine": {"type": "command",
                                        "command": f'node "{js_path}"', "refreshInterval": 30}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(existing, f)
            # The script file must actually exist on disk for this to be a true
            # no-op (see test_ours_correct_but_script_file_missing_falls_through_to_write
            # for the case where it doesn't).
            with open(js_path, "w", encoding="utf-8") as f:
                f.write("// existing script")
            code, prints = self._run(d, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertTrue(any("Already configured" in p for p in prints))
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), existing)  # untouched

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_ours_correct_but_script_file_missing_falls_through_to_write(self, _node):
        # Regression test for Finding 1: settings already match (refreshInterval
        # 30, "ours" command) but the .js file itself is missing from disk --
        # e.g. deleted, or a settings.json that arrived on a second device
        # without its companion script. The wizard must NOT claim "Already
        # configured" (the hook is actually broken -- node has nothing to run)
        # and must instead proceed to the write flow so it can be repaired.
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, STATUSLINE_MARKER)
            existing = {"statusLine": {"type": "command",
                                        "command": f'node "{js_path}"', "refreshInterval": 30}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(existing, f)
            self.assertFalse(os.path.exists(js_path))  # sanity: script truly absent

            code, prints = self._run(d, input_fn=lambda _: "y")

            self.assertEqual(code, 0)
            self.assertFalse(any("Already configured" in p for p in prints))
            self.assertTrue(any("This will write" in p for p in prints))
            self.assertTrue(os.path.exists(js_path))  # repaired

    @patch.object(statusline_mod, "node_available", return_value=False)
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

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_non_interactive_without_dry_run_aborts(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, isatty_fn=lambda: False,
                                      input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("not running interactively" in p.lower() for p in prints))

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_final_report_shown_after_successful_write(self, _node):
        with tempfile.TemporaryDirectory() as d:
            _, prints = self._run(d, input_fn=lambda _: "y")
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))
            self.assertTrue(any("Toast notifications:" in p for p in prints))

    @patch.object(statusline_mod, "node_available", return_value=True)
    @patch.object(statusline_mod, "write_settings_with_backup", side_effect=OSError("Permission denied"))
    def test_write_permission_error_reported_not_crashed(self, _write, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 1)
            self.assertTrue(any("permission denied" in p.lower() for p in prints))
            # the statusline script write happens before the settings write in
            # source order, so it may exist -- but settings.json must not, since
            # write_settings_with_backup is what raised before touching it
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))


class ForeignStatuslineTests(unittest.TestCase):
    def test_extract_script_path_from_quoted_command(self):
        self.assertEqual(
            _extract_script_path('node "C:\\Users\\x\\.claude\\statusline.js"'),
            "C:\\Users\\x\\.claude\\statusline.js")

    def test_extract_script_path_returns_none_when_unquoted(self):
        self.assertIsNone(_extract_script_path("~/.claude/statusline.sh"))

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_foreign_statusline_does_not_write_settings(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            original = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(original, f)
            prints = []
            code = run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                input_fn=lambda _: self.fail("must not prompt"),
                                                isatty_fn=lambda: True)
            self.assertEqual(code, 0)
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), original)
            self.assertFalse(os.path.exists(os.path.join(d, STATUSLINE_MARKER)))
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_foreign_statusline_reports_fresh_capture_if_present(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}, f)
            rl_path = os.path.join(d, "rate_limits_latest.json")
            with open(rl_path, "w", encoding="utf-8") as f:
                json.dump({"rate_limits": {}, "captured_at": time.time()}, f)
            prints = []
            with patch.object(statusline_mod, "RATE_LIMITS_PATH", rl_path):
                run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                             input_fn=lambda _: self.fail("must not prompt"),
                                             isatty_fn=lambda: True)
            self.assertTrue(any("already has a fresh capture" in p for p in prints))

    @patch.object(statusline_mod, "node_available", return_value=True)
    def test_foreign_statusline_catches_non_oserror_exceptions(self, _node):
        """Verify that non-OSError exceptions (e.g. ValueError from isfile) are caught."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": 'node "/bad/path"'}}, f)
            prints = []
            # Mock os.path.isfile to raise ValueError (simulating a path with null bytes)
            with patch.object(os.path, "isfile", side_effect=ValueError("null byte in path")):
                code = run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                    input_fn=lambda _: self.fail("must not prompt"),
                                                    isatty_fn=lambda: True)
            # Should complete without raising the ValueError
            self.assertEqual(code, 0)
            # Should still print the existing statusline message and final report
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))


class StartupHintTests(unittest.TestCase):
    def test_none_on_non_windows_even_if_missing(self):
        self.assertIsNone(startup_hint("/no/such/path.json", is_windows=False,
                                            statusline_js_path="/no/such/statusline.js"))

    def test_hint_when_windows_and_never_captured(self):
        hint = startup_hint("/no/such/path.json", is_windows=True,
                                 statusline_js_path="/no/such/statusline.js")
        self.assertIsNotNone(hint)
        self.assertIn("--setup-notifications", hint)

    def test_none_when_file_already_exists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rate_limits_latest.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertIsNone(startup_hint(path, is_windows=True,
                                                statusline_js_path="/no/such/statusline.js"))

    def test_none_when_statusline_script_already_exists(self):
        # right after a successful --setup-notifications run, rate_limits_latest.json
        # doesn't exist yet (it only appears after the next Claude Code message) --
        # but the managed script does, so the nudge would be confusing here and
        # must be suppressed.
        with tempfile.TemporaryDirectory() as d:
            js_path = os.path.join(d, STATUSLINE_MARKER)
            with open(js_path, "w", encoding="utf-8") as f:
                f.write("// managed")
            self.assertIsNone(startup_hint("/no/such/rate_limits.json", is_windows=True,
                                                statusline_js_path=js_path))


class PrintFinalReportTests(unittest.TestCase):
    def test_toast_on_when_enabled_and_windows(self):
        prints = []
        with patch.object(os, "name", "nt"), \
             patch.dict(NOTIFY, {"enabled": True}):
            _print_final_report(prints.append)
        self.assertTrue(any("Toast notifications: ON" in p for p in prints), prints)

    def test_toast_off_when_disabled_on_windows(self):
        prints = []
        with patch.object(os, "name", "nt"), \
             patch.dict(NOTIFY, {"enabled": False}):
            _print_final_report(prints.append)
        self.assertTrue(any(p == "Toast notifications: OFF"
                             '  (edit NOTIFY["enabled"] in config.py to change)'
                             for p in prints), prints)

    def test_toast_off_windows_only_on_non_windows(self):
        prints = []
        with patch.object(os, "name", "posix"), \
             patch.dict(NOTIFY, {"enabled": True}):
            _print_final_report(prints.append)
        self.assertTrue(any("Toast notifications: OFF (Windows only)" in p for p in prints), prints)


class ReportForeignStatuslineSizeGuardTests(unittest.TestCase):
    @staticmethod
    def _settings_for(command):
        return {"statusLine": {"type": "command", "command": command}}

    def test_skips_reading_a_large_foreign_script(self):
        with tempfile.TemporaryDirectory() as d:
            big_path = os.path.join(d, "huge.js")
            with open(big_path, "wb") as f:
                # the string IS present, so a guard-less read would find it --
                # this proves the size guard is what skips it, not the content
                f.write(b"// references rate_limits\n")
                f.seek(1_000_001)
                f.write(b"\0")
            prints = []
            _report_foreign_statusline(
                self._settings_for(f'node "{big_path}"'), prints.append)
            self.assertFalse(any("references rate_limits" in p for p in prints), prints)

    def test_still_reads_a_small_foreign_script(self):
        with tempfile.TemporaryDirectory() as d:
            small_path = os.path.join(d, "small.js")
            with open(small_path, "w", encoding="utf-8") as f:
                f.write("// writes rate_limits somewhere")
            prints = []
            _report_foreign_statusline(
                self._settings_for(f'node "{small_path}"'), prints.append)
            self.assertTrue(any("references rate_limits" in p for p in prints), prints)


class BundledStatuslineJsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_bundled_js_is_syntactically_valid(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "statusline.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(BUNDLED_STATUSLINE_JS)
            result = subprocess.run(["node", "--check", path],
                                     capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
