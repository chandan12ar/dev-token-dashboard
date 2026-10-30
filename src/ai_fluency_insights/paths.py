"""Filesystem locations: Claude Code's directories, bundled resources and per-user app data.

Nothing here writes next to the code, so the same logic works from a source
checkout, a pip install, or a frozen executable.
"""

import os
import shutil
import sys

APP_DIR_NAME = "AIFluencyInsights"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
# Before v2.0 the app was a single script and kept its state next to itself.
_LEGACY_STATE_FILE = ".notify_state.json"


def claude_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def default_root():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.join(env, "projects")
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def resource_path(*parts):
    """A file shipped inside the package (web UI, bundled scripts)."""
    return os.path.join(PACKAGE_DIR, *parts)


def app_data_dir():
    """Per-user writable directory for runtime state. AFI_DATA_DIR overrides it."""
    override = os.environ.get("AFI_DATA_DIR")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, APP_DIR_NAME)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_DIR_NAME)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "ai-fluency-insights")


def notify_state_path():
    """Where NotificationWatcher persists its progress; created on first use.

    Carries over the state file a pre-v2.0 source checkout kept at the repo root,
    so upgrading does not re-fire milestones that already went off."""
    data_dir = app_data_dir()
    path = os.path.join(data_dir, "notify_state.json")
    try:
        os.makedirs(data_dir, exist_ok=True)
        legacy = os.path.join(os.path.dirname(os.path.dirname(PACKAGE_DIR)), _LEGACY_STATE_FILE)
        if not os.path.exists(path) and os.path.isfile(legacy):
            shutil.copy2(legacy, path)
    except OSError:
        pass
    return path
