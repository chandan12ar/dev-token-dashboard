"""My Adoption engine, adoption levels and AI fluency scores."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from ai_fluency_insights.analytics.adoption import adoption_level, build_adoption
from ai_fluency_insights.analytics.fluency import fluency_of
from ai_fluency_insights.config import ADOPTION
from ai_fluency_insights.core.scanner import Scanner


class AdoptionTests(unittest.TestCase):
    """build_adoption(): the My Adoption tab's score, pillars, features and coach."""

    @staticmethod
    def _stamp(days_ago, hour=10, minute=0):
        t = (datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)
             - timedelta(days=days_ago))
        return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _session(self, sid, days_ago, tools=(), prompt="please add pagination to the orders list and keep the style",
                 hour=10, n=0):
        ts = self._stamp(days_ago, hour)
        es = [{"type": "user", "sessionId": sid, "timestamp": ts, "cwd": "/nonexistent/proj",
               "message": {"role": "user", "content": prompt}}]
        blocks = [{"type": "tool_use", "id": "tu_%s_%d" % (sid, i), "name": nm, "input": inp}
                  for i, (nm, inp) in enumerate(tools)]
        es.append({"type": "assistant", "sessionId": sid, "timestamp": ts, "uuid": "m_" + sid,
                   "requestId": "r_" + sid,
                   "message": {"id": "m_" + sid, "model": "claude-sonnet-5",
                               "content": blocks,
                               "usage": {"input_tokens": 10, "output_tokens": 500,
                                         "cache_creation_input_tokens": 0,
                                         "cache_read_input_tokens": 1000}}})
        return es

    def _build(self, sessions):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        proj = os.path.join(root, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, "a.jsonl"), "w", encoding="utf-8") as f:
            for es in sessions:
                for e in es:
                    f.write(json.dumps(e) + "\n")
        return build_adoption(Scanner(root).build_stats())

    def test_no_data_is_not_ready(self):
        a = self._build([])
        self.assertFalse(a["ready"])
        self.assertEqual(a["active_days"], 0)

    def test_baseline_needed_below_min_active_days(self):
        a = self._build([self._session("a", 0), self._session("b", 1)])
        self.assertFalse(a["ready"])
        self.assertEqual(a["active_days"], 2)
        self.assertEqual(a["min_days"], ADOPTION["min_active_days"])

    def test_ready_payload_shape_and_ranges(self):
        edit = ("Edit", {"file_path": "/x/a.py", "new_string": "\n".join("l%d" % i for i in range(40))})
        sessions = [self._session("s%d" % i, i * 2, tools=[edit, ("Bash", {"command": "pytest"})])
                    for i in range(8)]
        a = self._build(sessions)
        self.assertTrue(a["ready"])
        self.assertTrue(0 <= a["score"] <= 100)
        self.assertEqual([p["id"] for p in a["pillars"]], ["habit", "range", "craft", "impact"])
        for p in a["pillars"]:
            self.assertTrue(0 <= p["score"] <= 100)
        self.assertEqual(len(a["weekly"]), 12)
        self.assertEqual(len(a["calendar"]["cells"]), 17 * 7)
        self.assertLessEqual(len(a["coach"]), 3)
        self.assertEqual(a["level"]["name"], adoption_level(a["score"])["name"])
        # the overall score really is the weighted blend of the four pillars
        wt = ADOPTION["weights"]
        blend = sum(wt[p["id"]] * p["score"] for p in a["pillars"])
        self.assertLessEqual(abs(a["score"] - blend), 1)

    def test_feature_detection_from_tool_calls(self):
        tools = [("ExitPlanMode", {}), ("Agent", {"subagent_type": "Explore"}),
                 ("WebSearch", {"query": "x"}), ("mcp__github__list_issues", {}),
                 ("Skill", {"skill": "x"}), ("Bash", {"command": "ls"})]
        a = self._build([self._session("a", 0, tools=tools), self._session("b", 1),
                         self._session("c", 2)])
        used = {f["id"]: f["recent"] for f in a["range"]["features"]}
        for fid in ("plan", "subagents", "web", "mcp", "skills", "verify"):
            self.assertGreater(used[fid], 0, fid)
        self.assertEqual(used["slash"], 0)
        # unused features surface as coach suggestions, never more than two
        feats = [c for c in a["coach"] if c["feature"]]
        self.assertLessEqual(len(feats), 2)
        for c in feats:
            self.assertEqual(used[c["feature"]], 0)

    def test_slash_command_counts_as_a_feature(self):
        cmd = [{"type": "user", "sessionId": "z", "timestamp": self._stamp(0),
                "message": {"role": "user",
                            "content": "<command-name>/compact</command-name>"}}]
        a = self._build([cmd, self._session("b", 1), self._session("c", 2)])
        used = {f["id"]: f["recent"] for f in a["range"]["features"]}
        self.assertEqual(used["slash"], 1)

    def test_late_night_activity_triggers_wellbeing_coaching(self):
        sessions = [self._session("n%d" % i, i, hour=23 if i % 2 == 0 else 1) for i in range(6)]
        a = self._build(sessions)
        self.assertGreater(a["wellbeing"]["late_pct"], 20)
        self.assertTrue(any(c["area"] == "wellbeing" for c in a["coach"]))

    def test_old_activity_ages_out_of_the_window(self):
        a = self._build([self._session("a", 0), self._session("b", 1), self._session("c", 2),
                         self._session("old", 60, tools=[("WebSearch", {"query": "x"})])])
        used = {f["id"]: f["recent"] for f in a["range"]["features"]}
        self.assertEqual(used["web"], 0)
        self.assertEqual(next(f for f in a["range"]["features"] if f["id"] == "web")["ever"], True)


class AdoptionLevelAndFluencyTests(unittest.TestCase):
    def test_level_boundaries(self):
        names = {0: "Explorer", 19: "Explorer", 20: "Practitioner", 39: "Practitioner",
                 40: "Power user", 59: "Power user", 60: "Expert", 74: "Expert",
                 75: "Pro", 89: "Pro", 90: "Pro Max", 100: "Pro Max"}
        for score, name in names.items():
            self.assertEqual(adoption_level(score)["name"], name, score)

    def test_points_to_next_level(self):
        lv = adoption_level(65)
        self.assertEqual(lv["to_next"], 10)
        self.assertEqual(lv["next_name"], "Pro")
        self.assertEqual(adoption_level(95)["to_next"], 0)

    def test_pro_needs_every_pillar_above_the_floor(self):
        strong = {"habit": 90, "range": 80, "craft": 85, "impact": 80}
        self.assertEqual(adoption_level(80, strong)["name"], "Pro")
        weak = dict(strong, range=40)
        lv = adoption_level(80, weak)
        self.assertEqual(lv["name"], "Expert")
        self.assertIn("Range", lv["blocker"])

    def test_pro_max_needs_a_sustained_run(self):
        pillars = {"habit": 95, "range": 90, "craft": 92, "impact": 88}
        steady = [{"score": 91}] * 4
        self.assertEqual(adoption_level(93, pillars, steady)["name"], "Pro Max")
        lv = adoption_level(93, pillars, [{"score": 70}, {"score": 91}, {"score": 91}, {"score": 92}])
        self.assertEqual(lv["name"], "Pro")
        self.assertIn("4 weeks", lv["blocker"])
        self.assertEqual(adoption_level(93, pillars, [{"score": 95}])["name"], "Pro")

    def test_fluency_matches_documented_formulas(self):
        w = {"sessions": [{"loc": 10, "bash": 1}, {"loc": 5, "bash": 0}, {"loc": 0, "bash": 0}],
             "human_tokens": 100, "output": 2500, "prompts": 20, "prompt_words": 400,
             "corrections": 1, "interruptions": 1, "tool_errors": 1, "writes": 10}
        f = fluency_of(w)
        # delegation = 50*(2/3) + 50*min(1, 25/50) = 58.33
        self.assertEqual(f["delegation"], 58)
        # one-shot 0.9, avg 20 words -> sweet 1.0: 65*.9 + 35 = 93.5 -> 94
        self.assertEqual(f["description"], 94)
        # engaged = min(1, 2/max(1, 1.0)) = 1; err ctl = 0.9 -> 95
        self.assertEqual(f["discernment"], 95)
        # 1 of 2 code sessions verified
        self.assertEqual(f["diligence"], 50)

    def test_fluency_handles_empty_window(self):
        w = {"sessions": [], "human_tokens": 0, "output": 0, "prompts": 0, "prompt_words": 0,
             "corrections": 0, "interruptions": 0, "tool_errors": 0, "writes": 0}
        f = fluency_of(w)
        self.assertEqual((f["delegation"], f["description"], f["diligence"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
