"""Flow, Quality, Value, Data Health, metric catalog and Advisor engines."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from ai_fluency_insights.analytics.adoption import build_adoption
from ai_fluency_insights.analytics.advisor import build_advisor
from ai_fluency_insights.analytics.catalog import METRIC_CATALOG, catalog_payload
from ai_fluency_insights.analytics.flow import build_flow
from ai_fluency_insights.analytics.health import build_health
from ai_fluency_insights.analytics.quality import build_quality
from ai_fluency_insights.analytics.value import build_value
from ai_fluency_insights.core.scanner import Scanner
from ai_fluency_insights.paths import resource_path

# The dashboard page as the browser receives it: markup plus its script.
PAGE_SOURCE = "".join(
    open(resource_path(*p), encoding="utf-8").read()
    for p in (("web", "index.html"), ("web", "static", "js", "app.js")))


class _LogFixture(unittest.TestCase):
    """Builds Claude Code style logs with timed turns, tool calls and results."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "proj"))
        self.base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.sid = "s1"
        self.entries = []
        self.n = 0
        self.t = 0.0
        self.out_tokens = 100

    def _ts(self, off):
        return (self.base + timedelta(seconds=off)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _base(self, off, **kw):
        d = {"sessionId": self.sid, "timestamp": self._ts(off), "cwd": "/nonexistent/p", "version": "2.1.296"}
        d.update(kw)
        return d

    def prompt(self, text, gap=5):
        self.t += gap
        self.entries.append(self._base(self.t, type="user", message={"role": "user", "content": text}))

    def tool(self, name, inp, run=1.0, fail=False, text="ok", perm=None):
        self.n += 1
        tid = "tu%d" % self.n
        self.t += 1
        self.entries.append(self._base(self.t, type="assistant", uuid="m%d" % self.n, requestId="r%d" % self.n,
            message={"id": "m%d" % self.n, "model": "claude-sonnet-5", "stop_reason": "tool_use",
                     "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                     "usage": {"input_tokens": 10, "output_tokens": self.out_tokens, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 1000}}))
        self.t += run
        res = {"type": "tool_result", "tool_use_id": tid, "content": text}
        if fail:
            res["is_error"] = True
        e = self._base(self.t, type="user", message={"role": "user", "content": [res]})
        if perm:
            e["permissionDecision"] = perm
        self.entries.append(e)

    def end_turn(self, gap=2):
        self.n += 1
        self.t += gap
        self.entries.append(self._base(self.t, type="assistant", uuid="m%d" % self.n, requestId="r%d" % self.n,
            message={"id": "m%d" % self.n, "model": "claude-sonnet-5", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "done"}],
                     "usage": {"input_tokens": 10, "output_tokens": 50, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 1000}}))

    def edit(self, path="/p/src/a.py", n=3, **kw):
        self.tool("Edit", {"file_path": path, "new_string": "\n".join("x" for _ in range(n))}, **kw)

    def stats(self):
        with open(os.path.join(self.root, "proj", "a.jsonl"), "w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(json.dumps(e) + "\n")
        return Scanner(self.root).build_stats()


class FlowEngineTests(_LogFixture):
    def test_turn_time_steps_and_clean_flag(self):
        self.prompt("add pagination to the orders list")
        self.edit(run=2.0)
        self.end_turn(gap=3)
        st = self.stats()
        self.assertEqual(len(st.turns), 1)
        t = st.turns[0]
        # prompt at t=5; end_turn at 5 + (1+2) + 3 = 11  ->  6s
        self.assertAlmostEqual(t["secs"], 6.0, places=1)
        self.assertEqual(t["steps"], 1)
        self.assertFalse(t["err"] or t["intr"] or t["corrected"])

    def test_correction_marks_the_previous_turn(self):
        self.prompt("add pagination")
        self.end_turn()
        self.prompt("no, that's wrong, use the helper")
        self.end_turn()
        st = self.stats()
        self.assertTrue(st.turns[0]["corrected"])
        self.assertFalse(st.turns[1]["corrected"])

    def test_tool_latency_pairs_call_and_result(self):
        self.prompt("run it")
        self.tool("Bash", {"command": "pytest -q"}, run=4.0)
        self.end_turn()
        st = self.stats()
        rows = [r for r in st.tool_calls if r[2] == "pytest"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][3], 4.0, places=1)
        self.assertEqual(st.hc["tool_results_matched"], st.hc["tool_results"])

    def test_failure_episode_recovers_and_counts_steps(self):
        self.prompt("fix the build")
        self.tool("Bash", {"command": "npm run build"}, fail=True, text="Exit code 1\nerr")
        self.tool("Read", {"file_path": "/p/a.py"})
        self.tool("Bash", {"command": "npm run build"})
        self.end_turn()
        st = self.stats()
        self.assertEqual(len(st.episodes), 1)
        day, tool, fails, steps = st.episodes[0]
        self.assertEqual((tool, fails, steps), ("Bash", 1, 2))
        self.assertEqual(st.hc["exit_code_text"], 1)

    def test_three_failures_in_a_row_is_a_stuck_loop_and_unrecovered(self):
        self.prompt("fix the build")
        for _ in range(3):
            self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.end_turn()
        f = build_flow(self.stats())
        self.assertEqual((f["episodes"], f["stuck"], f["unrecovered"], f["recovered"]), (1, 1, 1, 0))
        self.assertEqual(f["recovery_pct"], 0)

    def test_flow_needs_five_turns_for_a_score(self):
        for i in range(4):
            self.prompt("task %d" % i)
            self.end_turn()
        self.assertIsNone(build_flow(self.stats())["score"])
        self.prompt("task 5")
        self.end_turn()
        f = build_flow(self.stats())
        self.assertIsNotNone(f["score"])
        self.assertEqual(f["clean_pct"], 100)

    def test_interrupted_turn_is_not_clean(self):
        self.prompt("do the thing")
        self.entries.append(self._base(self.t + 1, type="user",
                                       message={"role": "user", "content": "[Request interrupted by user]"}))
        self.prompt("do it differently")
        self.end_turn()
        st = self.stats()
        self.assertEqual(len(st.turns), 2)
        self.assertTrue(st.turns[0]["intr"])
        self.assertIsNone(st.turns[0]["secs"])


class QualityEngineTests(_LogFixture):
    def _session(self, edit_first=True):
        self.prompt("change it")
        if edit_first:
            self.edit()
            self.tool("Bash", {"command": "pytest -q"})
        else:
            self.tool("Bash", {"command": "pytest -q"})
            self.edit()
        self.end_turn()

    def test_verified_means_a_check_ran_after_the_last_edit(self):
        self._session(edit_first=True)
        q = build_quality(self.stats())
        self.assertEqual(q["edited_sessions"], 1)
        self.assertEqual(q["verified_pct"], 100)

    def test_check_before_the_edit_does_not_count(self):
        self._session(edit_first=False)
        q = build_quality(self.stats())
        self.assertEqual(q["verified_pct"], 0)

    def test_first_run_pass_and_fail_then_pass_recovery(self):
        self.prompt("fix")
        self.edit()
        self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.tool("Bash", {"command": "pytest"})
        self.end_turn()
        q = build_quality(self.stats())
        self.assertEqual(q["first_run_pass_pct"], 0)
        self.assertEqual(q["runs"]["test"]["runs"], 2)
        self.assertEqual((q["runs"]["test"]["fail_groups"], q["runs"]["test"]["recovered_groups"]), (1, 1))

    def test_tests_alongside_counts_test_file_edits(self):
        self.prompt("add feature with tests")
        self.edit("/p/src/a.py")
        self.edit("/p/tests/test_a.py")
        self.end_turn()
        q = build_quality(self.stats())
        self.assertEqual(q["tests_alongside_pct"], 100)

    def test_reedit_share_and_hotspot(self):
        self.prompt("iterate")
        for _ in range(6):
            self.edit("/p/src/hot.py")
        self.edit("/p/src/other.py")
        self.end_turn()
        q = build_quality(self.stats())
        # 7 edits, 5 of them revisit a file already edited this session
        self.assertEqual(q["reedit_pct"], 71)
        self.assertEqual(q["hotspots"][0]["file"], "hot.py")
        self.assertEqual(q["hotspots"][0]["edits"], 6)

    def test_safety_signals_and_permission_decisions(self):
        self.prompt("clean up")
        self.tool("Bash", {"command": "git push --force origin main"},
                  perm={"decision": "accept", "source": "user", "reasonType": "rule"})
        self.tool("Read", {"file_path": "/p/.env"})
        self.tool("Bash", {"command": "python3 - <<'EOF'\nx = 'rm -rf /'\nEOF"})
        self.end_turn()
        q = build_quality(self.stats())
        self.assertEqual(q["safety"], {"force_push": 1, "secret_file_touched": 1})
        self.assertEqual(q["permissions"], [{"decision": "accept", "source": "user", "n": 1}])

    def test_subagent_work_does_not_end_the_main_turn(self):
        self.prompt("research it")
        self.entries.append(self._base(self.t + 1, type="assistant", isSidechain=True, uuid="sc1", requestId="rsc",
            message={"id": "sc1", "model": "claude-sonnet-5", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "sub done"}],
                     "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}))
        self.t += 2
        self.end_turn(gap=10)
        st = self.stats()
        self.assertEqual(len(st.turns), 1)
        self.assertGreater(st.turns[0]["secs"], 10)


class ValueEngineTests(_LogFixture):
    def test_cache_savings_is_read_discount_minus_write_premium(self):
        self.prompt("hi")
        self.end_turn()
        st = self.stats()
        # one response reading 1000 cached tokens at sonnet $2/M input: 0.9 * 1000 * 2 / 1e6
        saved = sum(d["cache_savings"] for d in st.days.values())
        self.assertAlmostEqual(saved, 0.9 * 1000 * 2.0 / 1e6, places=9)

    def test_cost_per_100_loc_and_per_verified(self):
        self.out_tokens = 1_000_000          # $10 of output per tool response at sonnet pricing
        self.prompt("build")
        self.edit(n=50)
        self.tool("Bash", {"command": "pytest"})
        self.end_turn()
        v = build_value(self.stats())
        self.assertEqual(v["loc"], 50)
        self.assertEqual(v["verified_sessions"], 1)
        self.assertGreater(v["cost_per_verified"], 0)
        self.assertAlmostEqual(v["per_100_loc"], v["cost"] / 50 * 100, places=2)


class HealthAndCatalogTests(_LogFixture):
    def test_missing_signals_are_reported_not_zeroed(self):
        self.prompt("hi")
        self.end_turn()
        h = build_health(self.stats())
        caps = {c["id"]: c for c in h["capabilities"]}
        self.assertEqual(caps["perms"]["status"], "missing")
        self.assertEqual(caps["turns"]["status"], "ok")
        self.assertEqual(h["versions"][0]["version"], "2.1.296")

    def test_catalog_is_complete_and_every_badge_in_the_page_exists(self):
        ids = [m[0] for m in METRIC_CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        for m in METRIC_CATALOG:
            self.assertIn(m[3], ("observed", "derived", "proxy", "estimated"))
            self.assertTrue(all(m[1:3]) and m[4] and m[5])
        import re as _re
        used = set(_re.findall(r"cb\('([a-z_0-9]+)'\)", PAGE_SOURCE))
        used |= set(_re.findall(r",C\.\w+,'([a-z_0-9]+)'", PAGE_SOURCE))
        used |= set(_re.findall(r"insHero\([^,]+,[^,]+,[^,]+,'([a-z_0-9]+)'", PAGE_SOURCE))
        self.assertTrue(used, "no badge ids found in the page")
        self.assertFalse(used - set(ids), used - set(ids))

    def test_payloads_serialise_to_json(self):
        self.prompt("go")
        self.edit()
        self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.end_turn()
        st = self.stats()
        for payload in (build_flow(st), build_quality(st), build_value(st),
                        build_health(st), catalog_payload()):
            json.dumps(payload)


class AdvisorTests(_LogFixture):
    def _many_sessions(self, verified):
        for i in range(6):
            self.sid = "s%d" % i
            self.prompt("make the change number %d to the orders module with tests" % i)
            self.edit()
            if verified:
                self.tool("Bash", {"command": "pytest -q"})
            self.end_turn()
            self.base = self.base  # sessions share the day; time keeps advancing via self.t
        return self.stats()

    def test_unverified_work_triggers_the_verify_recommendation_with_evidence(self):
        st = self._many_sessions(verified=False)
        adv = build_advisor(st, build_adoption(st) or {})
        self.assertTrue(adv["ready"])
        verify = [r for r in adv["recs"] + adv["more"] if r["id"] == "verify"]
        self.assertEqual(len(verify), 1)
        self.assertIn("0%", verify[0]["evidence"])
        self.assertEqual(verify[0]["experiment"]["metric"], "verified_pct")
        self.assertIn("verified_pct", adv["metrics"])

    def test_verified_work_earns_a_win_not_a_warning(self):
        st = self._many_sessions(verified=True)
        adv = build_advisor(st, {"end": build_adoption(st).get("end") or date.today().isoformat()})
        self.assertFalse([r for r in adv["recs"] if r["id"] == "verify"])
        self.assertTrue(any("check the work" in w for w in adv["wins"]))


if __name__ == "__main__":
    unittest.main()
