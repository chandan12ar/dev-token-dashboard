"""NotificationWatcher: baselines, persistence, plan-window and official rate-limit toasts."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_fluency_insights.config import NOTIFY, should_run_notifications
from ai_fluency_insights.core.scanner import Scanner
from ai_fluency_insights.notifications import watcher as watcher_mod
from ai_fluency_insights.notifications.plan_usage import RATE_LIMITS_STALE_SECONDS
from ai_fluency_insights.notifications.state import load_notify_state, save_notify_state
from ai_fluency_insights.notifications.watcher import NotificationWatcher


class NotifyStatePersistenceTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(load_notify_state("/no/such/path.json"), {})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            state = {"sess-1": {"session_total": 12_345, "task_total": 0,
                                 "last_context_usage": 12_345}}
            save_notify_state(path, state)
            self.assertEqual(load_notify_state(path), state)


class NotificationWatcherTests(unittest.TestCase):
    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, input_tokens, output_tokens):
        return {"type": "assistant", "sessionId": sid,
                "message": {"usage": {"input_tokens": input_tokens,
                                       "output_tokens": output_tokens,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}}}

    def test_session_spanning_two_files_does_not_fire_on_first_poll(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-multi"
            # same session split across two log files, as can happen with
            # resumed/rotated Claude Code logs
            self._write_jsonl(os.path.join(proj, "a.jsonl"),
                               [self._usage_entry(sid, 20_000, 0)])
            self._write_jsonl(os.path.join(proj, "b.jsonl"),
                               [self._usage_entry(sid, 20_000, 0)])
            scanner = Scanner(root)
            notify = dict(NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = NotificationWatcher(
                scanner, notify=notify,
                state_path=os.path.join(root, "state.json"))
            watcher.poll_once()
            self.assertEqual(watcher.recent_events, [])

    def test_restart_does_not_reprocess_already_seen_entries(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-restart"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            state_path = os.path.join(root, "state.json")
            notify = dict(NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = Scanner(root)
            w1 = NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline poll: session_total becomes 5000

            # simulate a dashboard restart: a brand-new watcher and Scanner,
            # same on-disk logs and same persisted state file
            scanner2 = Scanner(root)
            w2 = NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            w2.poll_once()

            # no new entries since the restart, so w2 shouldn't need to
            # touch this session at all — its persisted total must survive
            # unchanged (not doubled) in the state file it re-saves
            saved = load_notify_state(state_path)
            self.assertEqual(saved["sessions"][sid]["session_total"], 5_000)
            self.assertEqual(w2.recent_events, [])

    def test_save_state_preserves_sessions_not_touched_this_poll(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-quiet"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            state_path = os.path.join(root, "state.json")
            notify = dict(NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = Scanner(root)
            w1 = NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # sid becomes known, session_total=5000

            # this poll only looks at files modified in the last 0 minutes,
            # so sid's file is now "outside the active window" and won't be
            # touched — its persisted total must not be lost
            scanner2 = Scanner(root)
            w2 = NotificationWatcher(
                scanner2, notify=dict(notify, active_window_min=0),
                state_path=state_path)
            w2.poll_once()

            saved = load_notify_state(state_path)
            self.assertEqual(saved["sessions"][sid]["session_total"], 5_000)

    @patch.object(watcher_mod, "send_windows_toast")
    def test_new_usage_after_baseline_fires_normally(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-1"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            scanner = Scanner(root)
            notify = dict(NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = NotificationWatcher(
                scanner, notify=notify,
                state_path=os.path.join(root, "state.json"))
            watcher.poll_once()
            self.assertEqual(watcher.recent_events, [])  # baseline poll

            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._usage_entry(sid, 6_000, 0)) + "\n")
            watcher.poll_once()
            kinds = [e["message"] for e in watcher.recent_events]
            self.assertTrue(any("11.0k" in m or "10.0k" in m for m in kinds),
                             kinds)


class PlanWindowNotificationTests(unittest.TestCase):
    """The plan-usage panel is GLOBAL (every session combined), unlike the
    per-session signals above."""

    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, input_tokens, output_tokens, ts):
        return {"type": "assistant", "sessionId": sid, "timestamp": ts,
                "message": {"usage": {"input_tokens": input_tokens,
                                       "output_tokens": output_tokens,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}}}

    @patch.object(watcher_mod, "send_windows_toast")
    def test_plan_pct_combines_sessions_and_coalesces_per_poll(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            path_a = os.path.join(proj, "a.jsonl")
            path_b = os.path.join(proj, "b.jsonl")
            self._write_jsonl(path_a, [self._usage_entry(
                "sess-a", 1_000, 0, "2026-01-01T00:00:00Z")])
            self._write_jsonl(path_b, [self._usage_entry(
                "sess-b", 1_000, 0, "2026-01-01T00:00:01Z")])
            notify = dict(NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")
            scanner = Scanner(root)
            watcher = NotificationWatcher(scanner, notify=notify, state_path=state_path)
            watcher.poll_once()  # baseline: both sessions newly discovered, silent
            self.assertEqual(watcher.recent_events, [])

            # new usage lands on both sessions between polls; combined they
            # cross 25/50/75% of the ceiling in one poll -> one toast, not three
            with open(path_a, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._usage_entry(
                    "sess-a", 4_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            with open(path_b, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._usage_entry(
                    "sess-b", 4_000, 0, "2026-01-01T00:01:01Z")) + "\n")
            watcher.poll_once()

            plan_events = [e for e in watcher.recent_events
                           if e["title"].startswith("Plan usage")]
            self.assertEqual(len(plan_events), 1, watcher.recent_events)
            self.assertIn("75%", plan_events[0]["message"])
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(watcher_mod, "send_windows_toast")
    def test_plan_window_state_persists_across_restart(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(
                "sess-1", 1_000, 0, "2026-01-01T00:00:00Z")])
            notify = dict(NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")

            scanner1 = Scanner(root)
            w1 = NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline: sess-1 newly discovered, plan window untouched

            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._usage_entry(
                    "sess-1", 3_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            w1.poll_once()  # sess-1 now known -> 3000 tokens added to the plan window

            saved = load_notify_state(state_path)
            self.assertEqual(saved["plan_window"]["window_total"], 3_000)

            # simulate restart: fresh watcher, same on-disk state
            scanner2 = Scanner(root)
            w2 = NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            self.assertEqual(w2.plan_tracker.window_total, 3_000)


class OfficialRateLimitNotificationTests(unittest.TestCase):
    """The official-source path (poll_once -> _poll_official_rate_limits and
    plan_window_snapshot) must prefer real Anthropic numbers over the local
    estimate whenever a fresh statusline capture exists, and must not refire
    milestones already seen before a restart."""

    def _watcher(self, root, rate_limits_path):
        scanner = Scanner(root)
        return NotificationWatcher(
            scanner, notify=dict(NOTIFY, active_window_min=999_999),
            state_path=os.path.join(root, "state.json"),
            rate_limits_path=rate_limits_path)

    def _write_rl(self, path, five_pct, seven_pct=None, captured_at=None):
        rl = {"five_hour": {"used_percentage": five_pct, "resets_at": 9_999_999_999}}
        if seven_pct is not None:
            rl["seven_day"] = {"used_percentage": seven_pct, "resets_at": 9_999_999_999}
        save_notify_state(path, {
            "rate_limits": rl,
            "captured_at": captured_at if captured_at is not None else time.time(),
        })

    def test_snapshot_prefers_official_over_estimate_when_fresh(self):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 81, seven_pct=11)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "official")
            self.assertEqual(snap["pct"], 81)
            self.assertEqual(snap["week_pct"], 11)

    def test_snapshot_reports_capture_age_for_transparency(self):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 81, seven_pct=11, captured_at=time.time() - 300)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["captured_age_min"], 5)

    @patch.object(watcher_mod, "send_windows_toast")
    def test_85pct_milestone_fires_not_90(self, mock_toast):
        # the dashboard's requested milestones are 25/50/75/85/100, not the
        # generic MILESTONES default of ...90... from earlier iterations
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 80)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 80%

            self._write_rl(rl_path, 87)  # crosses 85%, not 90%
            w.poll_once()

            official_events = [e for e in w.recent_events if e["id"].startswith("official:")]
            self.assertEqual(len(official_events), 1, w.recent_events)
            self.assertIn("85%", official_events[0]["message"])

    @patch.object(watcher_mod, "send_windows_toast")
    def test_weekly_uses_20_40_60_80_100_not_5h_milestones(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 10, seven_pct=10)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline: five_hour=10%, seven_day=10%

            # 10 -> 41%: crosses the weekly 20% and 40% milestones, but no
            # 5h-style milestone (25/50/75/85/100) up to 41
            self._write_rl(rl_path, 10, seven_pct=41)
            w.poll_once()

            weekly_events = [e for e in w.recent_events if e["id"].startswith("official:seven_day")]
            self.assertEqual(len(weekly_events), 1, w.recent_events)
            self.assertIn("40%", weekly_events[0]["message"])

    @patch.object(watcher_mod, "send_windows_toast")
    def test_5h_and_weekly_milestones_are_independent(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 20, seven_pct=15)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline

            # five_hour 20->26 crosses its 25% milestone; seven_day 15->22
            # crosses its 20% milestone -- both should fire independently
            self._write_rl(rl_path, 26, seven_pct=22)
            w.poll_once()

            ids = sorted(e["id"] for e in w.recent_events if e["id"].startswith("official:"))
            self.assertEqual(ids, ["official:five_hour:25", "official:seven_day:20"])

    def test_snapshot_falls_back_to_estimate_only_when_never_captured(self):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")  # never written -> missing
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "estimate")

    def test_snapshot_prefers_stale_official_over_local_estimate(self):
        # A real (if hours-old) Anthropic percentage is still more
        # trustworthy than PlanWindowTracker's ceiling guess -- a stale
        # capture should not make the dashboard fall all the way back to
        # the local estimate.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            stale_at = time.time() - RATE_LIMITS_STALE_SECONDS - 3600
            self._write_rl(rl_path, 42, seven_pct=17, captured_at=stale_at)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "official")
            self.assertEqual(snap["pct"], 42)
            self.assertEqual(snap["week_pct"], 17)
            self.assertGreaterEqual(snap["captured_age_min"], 60)

    def test_snapshot_treats_a_stale_capture_past_its_reset_as_zero(self):
        # If the window's resets_at has already passed, the account-side
        # window has reset even though our last capture predates that --
        # showing the old leftover percentage here would be worse than
        # showing nothing.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 91,
                                               "resets_at": time.time() - 60}},
                "captured_at": time.time() - RATE_LIMITS_STALE_SECONDS - 3600,
            })
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "official")
            self.assertEqual(snap["pct"], 0)

    @patch.object(watcher_mod, "send_windows_toast")
    def test_first_sighting_sets_baseline_without_firing(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 60)
            w = self._watcher(root, rl_path)
            w.poll_once()
            self.assertEqual(w.recent_events, [])
            mock_toast.assert_not_called()

    @patch.object(watcher_mod, "send_windows_toast")
    def test_crossing_a_milestone_fires_once(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 60)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 60%

            self._write_rl(rl_path, 76)  # crosses 75%
            w.poll_once()

            official_events = [e for e in w.recent_events if e["id"].startswith("official:")]
            self.assertEqual(len(official_events), 1, w.recent_events)
            self.assertIn("75%", official_events[0]["message"])
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(watcher_mod, "send_windows_toast")
    def test_same_window_does_not_refire_on_noisy_official_pct(self, mock_toast):
        # Anthropic's reported used_percentage for the *same* window (same
        # resets_at) can wobble by a point or two between statusline
        # captures rather than climbing monotonically. Once 85% has fired,
        # dipping back to 84% and re-crossing 85% moments later — still
        # inside the same window — must not fire a second toast; that's
        # exactly the "same alert 6-8 times in a row" bug, not a real new
        # crossing like a context compaction resetting usage.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 80)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 80%

            self._write_rl(rl_path, 87)  # crosses 85%
            w.poll_once()
            self.assertEqual(mock_toast.call_count, 1)

            self._write_rl(rl_path, 84)  # wobbles back under 85%
            w.poll_once()
            self._write_rl(rl_path, 86)  # ...and back over — same window
            w.poll_once()
            self._write_rl(rl_path, 84)
            w.poll_once()
            self._write_rl(rl_path, 85)
            w.poll_once()

            official_events = [e for e in w.recent_events if e["id"].startswith("official:")]
            self.assertEqual(len(official_events), 1, w.recent_events)
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(watcher_mod, "send_windows_toast")
    def test_new_window_refires_milestones_from_zero(self, mock_toast):
        # A genuinely new 5h window (different resets_at) must be free to
        # fire 25/50/75/85 again from scratch, even though the previous
        # window already fired them all.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            resets1 = time.time() + 1000
            save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": resets1}},
                "captured_at": time.time()})
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 80%

            save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 87, "resets_at": resets1}},
                "captured_at": time.time()})
            w.poll_once()  # crosses 85% in window 1
            self.assertEqual(mock_toast.call_count, 1)

            resets2 = resets1 + 5 * 3600
            save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": resets2}},
                "captured_at": time.time()})
            w.poll_once()  # new window: crosses 25%
            self.assertEqual(mock_toast.call_count, 2)

            official_events = [e for e in w.recent_events if e["id"] == "official:five_hour:25"]
            self.assertEqual(len(official_events), 1, w.recent_events)

    @patch.object(watcher_mod, "send_windows_toast")
    def test_restart_does_not_refire_already_crossed_milestone(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            state_path = os.path.join(root, "state.json")
            self._write_rl(rl_path, 60)
            scanner1 = Scanner(root)
            w1 = NotificationWatcher(
                scanner1, notify=dict(NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w1.poll_once()  # baseline at 60%

            self._write_rl(rl_path, 76)  # crosses 75%
            w1.poll_once()
            self.assertEqual(mock_toast.call_count, 1)

            # restart: fresh watcher loads the persisted official_prev=76,
            # same 76% value on disk -> no new crossing, no refire
            scanner2 = Scanner(root)
            w2 = NotificationWatcher(
                scanner2, notify=dict(NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w2.poll_once()
            self.assertEqual(w2.recent_events, [])
            self.assertEqual(mock_toast.call_count, 1)


class ShouldRunNotificationsTests(unittest.TestCase):
    def test_true_by_default(self):
        self.assertTrue(should_run_notifications(dict(NOTIFY, enabled=True)))

    def test_false_when_disabled(self):
        self.assertFalse(should_run_notifications(dict(NOTIFY, enabled=False)))

    def test_true_when_key_absent(self):
        notify = dict(NOTIFY)
        del notify["enabled"]
        self.assertTrue(should_run_notifications(notify))

    def test_defaults_to_module_level_notify(self):
        with patch.dict(NOTIFY, {"enabled": False}):
            self.assertFalse(should_run_notifications())


if __name__ == "__main__":
    unittest.main()
