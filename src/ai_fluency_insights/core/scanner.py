"""Incremental scanner over ~/.claude/projects (re-reads only files whose mtime changed)."""

import json
import os
import threading

from .parser import parse_entry
from .stats import Stats

# ---------------------------------------------------------------------------
# Incremental scanner (re-reads only files whose mtime changed)
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self, root):
        self.root = root
        self._cache = {}     # path -> (mtime, size)
        self._entries = {}   # path -> list[dict]
        self._lock = threading.Lock()

    def scan(self):
        found = set()
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, fn)
                found.add(path)
                try:
                    stat = os.stat(path)
                    key = (stat.st_mtime, stat.st_size)
                except OSError:
                    continue
                if self._cache.get(path) == key:
                    continue
                entries = []
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    continue
                self._cache[path] = key
                self._entries[path] = entries
        # drop deleted files
        for path in list(self._entries):
            if path not in found:
                self._entries.pop(path, None)
                self._cache.pop(path, None)

    def build_stats(self, min_day=None, max_day=None) -> Stats:
        with self._lock:
            self.scan()
            st = Stats()
            for path, entries in self._entries.items():
                project = os.path.basename(os.path.dirname(path)) or "unknown"
                # prettify: Claude Code encodes the cwd in the dir name
                project = project.lstrip("-").replace("--", "/").replace("-", "/")
                for e in entries:
                    try:
                        parse_entry(e, project, st, min_day, max_day)
                    except Exception:
                        st.parse_errors += 1
            if st.parse_errors:
                print(f"[warn] {st.parse_errors} log entries could not be parsed "
                      "(log format may have changed - stats may be incomplete)")
            return st
