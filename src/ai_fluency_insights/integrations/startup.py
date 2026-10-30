"""Windows auto-start at logon (--install-startup / --uninstall-startup) via Task Scheduler."""

import os
import subprocess
import sys
import tempfile

# Windows logon task names: current, and the pre-rename one (cleaned up on install/uninstall).
STARTUP_TASK = "AIFluencyInsights"
LEGACY_STARTUP_TASK = "DevTokenDashboard"


def launch_command(port, executable=None, frozen=None):
    """The command line the logon task runs: the .exe itself when frozen, else `pythonw -m`."""
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return f'"{executable}" --no-browser --port {port}'
    pyw = executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = executable
    return f'"{pyw}" -m ai_fluency_insights --no-browser --port {port}'


def _package_importable():
    """True if `python -m ai_fluency_insights` works from outside the source tree."""
    r = subprocess.run([sys.executable, "-c", "import ai_fluency_insights"],
                       cwd=tempfile.gettempdir(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def install_startup(port):
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not getattr(sys, "frozen", False) and not _package_importable():
        print("The package isn't installed, so a logon task couldn't start it.")
        print("Install it first:  pip install .   (from the repository root)")
        return 1
    # Drop the pre-rename task so two copies don't start at logon.
    subprocess.run(["schtasks", "/Delete", "/TN", LEGACY_STARTUP_TASK, "/F"], **quiet)
    r = subprocess.run(["schtasks", "/Create", "/TN", STARTUP_TASK, "/TR", launch_command(port),
                        "/SC", "ONLOGON", "/F"])
    if r.returncode == 0:
        print(f"Installed: dashboard will start at logon on port {port}.")
        print("Remove anytime with:  ai-fluency-insights --uninstall-startup")
        return 0
    print("Failed — try running the terminal as Administrator.")
    return 1


def uninstall_startup():
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["schtasks", "/Delete", "/TN", STARTUP_TASK, "/F"])
    subprocess.run(["schtasks", "/Delete", "/TN", LEGACY_STARTUP_TASK, "/F"], **quiet)
    print("Removed logon task.")
    return 0
