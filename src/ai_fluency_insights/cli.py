"""Command-line entry point: `ai-fluency-insights`, `python -m ai_fluency_insights`, or the .exe."""

import argparse
import json
import os
import sys

from . import __version__
from .analytics.summary import stats_to_json
from .core.scanner import Scanner
from .integrations.startup import install_startup, uninstall_startup
from .integrations.statusline import run_setup_notifications
from .paths import default_root


def build_parser():
    ap = argparse.ArgumentParser(prog="ai-fluency-insights",
                                 description="Local Claude Code usage dashboard (zero token cost)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--dir", default=default_root(), help="Claude projects log dir")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dump", action="store_true", help="print stats JSON and exit (for testing)")
    ap.add_argument("--install-startup", action="store_true",
                    help="Windows: run dashboard automatically at logon (Task Scheduler)")
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="Windows: remove the logon task")
    ap.add_argument("--setup-notifications", action="store_true",
                    help="Wire up the statusLine hook for accurate plan-usage %% "
                         "(toast notifications: Windows only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --setup-notifications, show what would change "
                         "without writing anything")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.setup_notifications:
        sys.exit(run_setup_notifications(dry_run=args.dry_run))

    if args.install_startup or args.uninstall_startup:
        if os.name != "nt":
            sys.exit("Startup install is Windows-only.")
        sys.exit(uninstall_startup() if args.uninstall_startup else install_startup(args.port))

    if not os.path.isdir(args.dir):
        print(f"[!] Log directory not found: {args.dir}")
        print("    Pass it explicitly:  ai-fluency-insights --dir <path>")
        sys.exit(1)

    if args.dump:
        print(json.dumps(stats_to_json(Scanner(args.dir).build_stats(), None), indent=2))
        return

    from .server.app import serve
    serve(args.dir, args.port, open_browser=not args.no_browser)
