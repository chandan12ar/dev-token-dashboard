"""Setup wizard (--setup-notifications): wires Claude Code's statusLine hook."""

import json
import os
import re
import shutil
import sys
import time

from ..config import should_run_notifications
from ..notifications.plan_usage import RATE_LIMITS_PATH, load_rate_limits
from ..paths import claude_config_dir, resource_path

# ---------------------------------------------------------------------------
# Setup wizard (--setup-notifications): wires the statusLine hook so plan-
# usage % and toast milestones use real Anthropic numbers instead of the
# local estimate. See docs/superpowers/specs/2026-09-09-setup-notifications-
# onboarding-design.md for the full design.
# ---------------------------------------------------------------------------

STATUSLINE_MARKER = "ai_fluency_insights_statusline.js"
# Installs made before the rename point at this file; still treated as ours.
LEGACY_STATUSLINE_MARKER = "dev_token_dashboard_statusline.js"


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
    command = sl.get("command") if isinstance(sl, dict) else None
    if not isinstance(command, str) or not command:
        return "missing"
    if STATUSLINE_MARKER in command or LEGACY_STATUSLINE_MARKER in command:
        return "ours"
    return "foreign"


def merge_statusline_config(settings, statusline_js_path):
    new_settings = dict(settings)
    kind = classify_statusline(settings)
    if kind == "missing":
        new_settings["statusLine"] = {
            "type": "command",
            "command": f'node "{statusline_js_path}"',
            "refreshInterval": 30,
        }
    else:  # "ours" -- caller must not call this for "foreign"
        sl = dict(settings["statusLine"], refreshInterval=30)
        if LEGACY_STATUSLINE_MARKER in sl["command"]:
            # Pre-rename install: point it at the new script path.
            sl["command"] = f'node "{statusline_js_path}"'
        new_settings["statusLine"] = sl
    return new_settings


def node_available():
    return shutil.which("node") is not None


def write_settings_with_backup(settings_path, new_settings):
    backup_path = None
    if os.path.exists(settings_path):
        backup_path = f"{settings_path}.bak-{int(time.time())}"
        shutil.copy2(settings_path, backup_path)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new_settings, f, indent=2)
    os.replace(tmp, settings_path)
    return backup_path


# resources/statusline.js, written verbatim to
# <claude_config_dir>/ai_fluency_insights_statusline.js by
# run_setup_notifications() -- never to statusline.js or any user-chosen path.
# Trimmed from the hand-written ~/.claude/statusline.js already in use on the
# maintainer's machine: same merge-onto-previous-snapshot side effect, plus a
# minimal one-line status output for users who had no statusLine at all.
with open(resource_path("resources", "statusline.js"), "r", encoding="utf-8") as _f:
    BUNDLED_STATUSLINE_JS = _f.read()


def _print_final_report(print_fn):
    fresh = load_rate_limits(RATE_LIMITS_PATH) is not None
    print_fn("")
    print_fn("Rate-limit capture:  " + ("ok, fresh" if fresh else
              "not yet captured (will appear after your next Claude Code message)"))
    if os.name != "nt":
        print_fn("Toast notifications: OFF (Windows only)")
    else:
        print_fn("Toast notifications: " + ("ON" if should_run_notifications() else "OFF") +
                  '  (edit NOTIFY["enabled"] in config.py to change)')


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
    if new_settings == settings and os.path.isfile(statusline_js_path):
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


def _extract_script_path(command):
    m = re.search(r'"([^"]+)"', command)
    return m.group(1) if m else None


def _report_foreign_statusline(settings, print_fn):
    cmd = settings["statusLine"]["command"]
    print_fn(f"An existing statusLine is already configured: {cmd}")
    script_path = _extract_script_path(cmd)
    try:
        if (script_path and os.path.isfile(script_path)
                and os.path.getsize(script_path) < 1_000_000):
            with open(script_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "rate_limits" in content:
                print_fn("Your existing script already looks like it references rate_limits.")
    except Exception:
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


def startup_hint(rate_limits_path=None, is_windows=None, statusline_js_path=None):
    rate_limits_path = rate_limits_path or RATE_LIMITS_PATH
    is_windows = (os.name == "nt") if is_windows is None else is_windows
    statusline_js_path = statusline_js_path or os.path.join(claude_config_dir(), STATUSLINE_MARKER)
    # Right after a successful --setup-notifications run, rate_limits_latest.json
    # doesn't exist yet (only the *next* Claude Code message populates it), but
    # our managed script does -- checking for it too avoids nudging someone who
    # just finished the setup this tip is telling them to run.
    if (is_windows and not os.path.exists(rate_limits_path)
            and not os.path.exists(statusline_js_path)):
        return "  Tip: plan-usage % is a local estimate. Run --setup-notifications for real Anthropic numbers."
    return None
