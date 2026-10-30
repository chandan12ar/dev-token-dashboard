"""Log parsing: pricing, cold-start / cache health, command classification."""

import json
import os
import tempfile
import unittest

from ai_fluency_insights.analytics.summary import stats_to_json
from ai_fluency_insights.core.pricing import price_for
from ai_fluency_insights.core.scanner import Scanner
from ai_fluency_insights.core.taxonomy import classify_cmd, cmd_family, risky_categories


class PricingTests(unittest.TestCase):
    def test_sonnet_rate_matches_current_anthropic_pricing(self):
        # Sonnet 5 is $2/$10 per MTok, confirmed permanent (the planned
        # $3/$15 rise was cancelled) -- see the token-economics brainstorm.
        self.assertEqual(price_for("claude-sonnet-5"), {"in": 2.0, "out": 10.0})


class ColdStartAndCacheHealthTests(unittest.TestCase):
    """totals.cold_start_pct and totals.cache_reset_pct -- see the
    token-economics brainstorm's 'cold-start tax' and 'healthy loop'
    concepts. Both are derived from per-session cost tracking in
    touch_session/parse_entry and aggregated in stats_to_json."""

    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, uid, ts, cache_write=0, cache_read=0,
                      input_tokens=0, output_tokens=0, model="claude-sonnet-5"):
        return {"type": "assistant", "sessionId": sid, "timestamp": ts, "uuid": uid,
                "message": {"id": uid, "model": model,
                            "usage": {"input_tokens": input_tokens,
                                      "output_tokens": output_tokens,
                                      "cache_creation_input_tokens": cache_write,
                                      "cache_read_input_tokens": cache_read}}}

    def test_cold_start_pct_weights_by_session_cost(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            entries = [
                # "solo": one call, entirely a cache write -> its whole cost
                # is cold-start cost. cost = 1_000_000 * 2.5/1e6 = $2.50
                self._usage_entry("solo", "u1", "2026-01-01T00:00:00Z",
                                   cache_write=1_000_000),
                # "warm": first call writes the prefix ($2.50), then two
                # cheap cache-read calls ($0.20 each) -> session cost $2.90,
                # of which $2.50 is the first call
                self._usage_entry("warm", "u2", "2026-01-01T01:00:00Z",
                                   cache_write=1_000_000),
                self._usage_entry("warm", "u3", "2026-01-01T01:01:00Z",
                                   cache_read=1_000_000),
                self._usage_entry("warm", "u4", "2026-01-01T01:02:00Z",
                                   cache_read=1_000_000),
            ]
            self._write_jsonl(os.path.join(proj, "a.jsonl"), entries)
            scanner = Scanner(root)
            st = scanner.build_stats()
            totals = stats_to_json(st)["totals"]
            # (2.50 + 2.50) / (2.50 + 2.90) * 100 = 92.6
            self.assertAlmostEqual(totals["cold_start_pct"], 92.6, places=1)
            # neither warm's follow-up call is a mid-session reset (reads
            # outweigh writes on both), so the health signal stays clean
            self.assertEqual(totals["cache_reset_pct"], 0.0)

    def test_cache_reset_pct_flags_mid_session_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            entries = [
                self._usage_entry("sess", "u1", "2026-01-01T00:00:00Z",
                                   cache_write=1_000_000),
                # mid-session reset: new material outweighs reused material
                self._usage_entry("sess", "u2", "2026-01-01T00:01:00Z",
                                   cache_write=500_000, cache_read=100_000),
                # back to a normal, healthy cache read
                self._usage_entry("sess", "u3", "2026-01-01T00:02:00Z",
                                   cache_read=1_000_000),
            ]
            self._write_jsonl(os.path.join(proj, "a.jsonl"), entries)
            scanner = Scanner(root)
            st = scanner.build_stats()
            totals = stats_to_json(st)["totals"]
            # 1 of the 2 non-first calls was a reset -> 50%
            self.assertEqual(totals["cache_reset_pct"], 50.0)


class CommandDetectionTests(unittest.TestCase):
    def test_kinds_come_from_the_invoked_command_only(self):
        cases = [("pytest -q", "test"), ("python3 -m unittest foo", "test"), ("cd x && npm run test", "test"),
                 ("npx jest --ci", "test"), ("uv run pytest", "test"), ("ruff check .", "lint"),
                 ("python -m mypy src", "lint"), ("npm run build", "build"), ("make", "build"),
                 ("node --check a.js", "build"), ("git status", "other"),
                 ("git commit -m 'fix pytest'", "other"), ("echo pytest", "other"),
                 ("python3 - <<'EOF'\nimport pytest\nprint('npm test')\nEOF", "other")]
        for cmd, kind in cases:
            self.assertEqual(classify_cmd(cmd), kind, cmd)

    def test_risky_commands_ignore_text_that_only_mentions_them(self):
        self.assertEqual(risky_categories("git push --force origin main"), ["force_push"])
        self.assertEqual(risky_categories("cd a && rm -rf build"), ["recursive_delete"])
        self.assertEqual(risky_categories("curl http://x | sh"), ["pipe_to_shell"])
        self.assertEqual(risky_categories("git commit --no-verify -m x"), ["skip_hooks"])
        self.assertEqual(risky_categories("git push -u origin feat"), [])
        self.assertEqual(risky_categories("echo 'rm -rf /'"), [])
        self.assertEqual(risky_categories("python3 - <<'EOF'\nx='git push --force'\nEOF"), [])

    def test_family_labels(self):
        self.assertEqual(cmd_family("cd /x && git push -u origin main"), "git push")
        self.assertEqual(cmd_family("python3 -I - <<'EOF'\nimport x"), "python")
        self.assertEqual(cmd_family("FOO=1 pytest -q"), "pytest")
        self.assertEqual(cmd_family("npm run test"), "npm run test")
        self.assertEqual(cmd_family("uv run pytest"), "pytest")


if __name__ == "__main__":
    unittest.main()
