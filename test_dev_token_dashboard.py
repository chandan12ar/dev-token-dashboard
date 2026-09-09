"""Tests for the token-usage notification feature in dev_token_dashboard.py.

Run with:  python -m unittest test_dev_token_dashboard -v
"""
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import dev_token_dashboard as dtd


class MilestonesCrossedTests(unittest.TestCase):
    def test_single_milestone_crossed(self):
        # 50000/200000 = 25%
        self.assertEqual(dtd.milestones_crossed(0, 50_000, 200_000), [25])

    def test_no_milestone_crossed_when_still_below(self):
        self.assertEqual(dtd.milestones_crossed(0, 10_000, 200_000), [])

    def test_multiple_milestones_crossed_in_one_jump(self):
        self.assertEqual(dtd.milestones_crossed(0, 200_000, 200_000),
                          [25, 50, 75, 85, 100])

    def test_already_past_milestone_does_not_refire(self):
        # prev is already above 50% threshold -> no new crossing
        self.assertEqual(dtd.milestones_crossed(120_000, 130_000, 200_000), [])

    def test_zero_total_disables_milestones(self):
        self.assertEqual(dtd.milestones_crossed(0, 999_999, 0), [])

    def test_drop_then_regrowth_refires_same_milestone(self):
        # simulates a context compaction: usage drops, then climbs back past 50%
        self.assertEqual(dtd.milestones_crossed(90_000, 110_000, 200_000), [50])


class StepsCrossedTests(unittest.TestCase):
    def test_single_step_crossed(self):
        self.assertEqual(dtd.steps_crossed(8_000, 12_000, 10_000), [10_000])

    def test_multiple_steps_crossed_in_one_jump(self):
        self.assertEqual(dtd.steps_crossed(0, 25_000, 10_000),
                          [10_000, 20_000])

    def test_no_step_crossed_when_still_below(self):
        self.assertEqual(dtd.steps_crossed(1_000, 4_999, 5_000), [])

    def test_no_step_crossed_on_no_increase(self):
        self.assertEqual(dtd.steps_crossed(10_000, 10_000, 5_000), [])

    def test_zero_step_disables(self):
        self.assertEqual(dtd.steps_crossed(0, 999_999, 0), [])


class WeightedTokensTests(unittest.TestCase):
    """Plan-window accounting must track quota-weighted usage, not a raw
    token sum — cache reads are ~0.1x the cost of fresh input (see PRICING),
    so a chat-heavy session with heavy tool-call caching would otherwise look
    like it burned far more quota than it actually did."""

    def test_input_and_output_weighted_by_pricing_ratio(self):
        # every PRICING tier prices output at 5x input, so output carries a
        # flat 5x weight regardless of model
        self.assertEqual(dtd.weighted_tokens(1_000, 100, 0, 0), 1_000 + 500)

    def test_cache_write_weighted_at_1_25x(self):
        self.assertEqual(dtd.weighted_tokens(0, 0, 1_000, 0), 1_250)

    def test_cache_read_weighted_at_0_1x(self):
        self.assertEqual(dtd.weighted_tokens(0, 0, 0, 1_000), 100)


class SessionTokenTrackerTests(unittest.TestCase):
    def make_tracker(self, **notify_overrides):
        notify = dict(dtd.NOTIFY)
        notify.update(notify_overrides)
        return dtd.SessionTokenTracker(notify=notify)

    def test_context_pct_event_on_usage(self):
        t = self.make_tracker(context_window=100_000,
                               session_step=0, task_step=0)
        events = t.on_usage(input_tokens=25_000, output_tokens=1_000,
                             cache_write=0, cache_read=0)
        self.assertIn(("context_pct", 25, 25_000), events)

    def test_session_step_event_on_cumulative_usage(self):
        t = self.make_tracker(context_window=0,
                               session_step=10_000, task_step=0)
        t.on_usage(8_000, 0, 0, 0)
        events = t.on_usage(4_000, 0, 0, 0)
        self.assertIn(("session_step", 10_000, 12_000), events)

    def test_task_step_resets_on_new_user_message(self):
        t = self.make_tracker(context_window=0,
                               session_step=0, task_step=5_000)
        events = t.on_usage(6_000, 0, 0, 0)
        self.assertIn(("task_step", 5_000, 6_000), events)
        t.on_user_message()
        # after reset, a small usage shouldn't immediately refire the step
        events2 = t.on_usage(1_000, 0, 0, 0)
        self.assertEqual(events2, [])

    def test_resuming_from_saved_state_does_not_refire(self):
        # simulates restart: tracker rebuilt from persisted totals
        t = dtd.SessionTokenTracker(notify=dict(dtd.NOTIFY, context_window=0,
                                                 session_step=10_000,
                                                 task_step=0),
                                     session_total=9_500)
        events = t.on_usage(400, 0, 0, 0)  # crosses to 9900, still under 10k
        self.assertEqual(events, [])


class PlanWindowTrackerTests(unittest.TestCase):
    """Approximates the Pro/Max app's rolling ~5h 'Current session' limit:
    a single global window (not per Claude Code session) that starts on
    first usage and resets fully 5h later."""

    WINDOW_SECONDS = 5 * 3600

    def test_first_event_starts_window_and_may_cross_milestone(self):
        t = dtd.PlanWindowTracker(ceiling=100_000)
        events = t.add(ts_epoch=1_000, tokens=30_000)
        self.assertEqual(events, [25])
        self.assertEqual(t.window_start, 1_000)
        self.assertEqual(t.window_total, 30_000)

    def test_second_event_within_window_accumulates(self):
        t = dtd.PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 30_000)
        events = t.add(1_000 + 3600, 30_000)  # 60% total now
        self.assertEqual(events, [50])
        self.assertEqual(t.window_total, 60_000)

    def test_event_at_exactly_5h_starts_a_new_window(self):
        t = dtd.PlanWindowTracker(ceiling=100_000)
        t.add(0, 90_000)
        events = t.add(self.WINDOW_SECONDS, 10_000)
        self.assertEqual(t.window_start, self.WINDOW_SECONDS)
        self.assertEqual(t.window_total, 10_000)
        self.assertEqual(events, [])  # 10% of the fresh window, no crossing

    def test_event_just_under_5h_stays_in_same_window(self):
        t = dtd.PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        t.add(self.WINDOW_SECONDS - 1, 10_000)
        self.assertEqual(t.window_start, 0)
        self.assertEqual(t.window_total, 20_000)

    def test_zero_ceiling_disables_milestones(self):
        t = dtd.PlanWindowTracker(ceiling=0)
        events = t.add(0, 50_000)
        self.assertEqual(events, [])

    def test_pct_caps_at_100(self):
        t = dtd.PlanWindowTracker(ceiling=100_000)
        t.add(0, 150_000)
        self.assertEqual(t.pct(), 100)

    def test_resets_in_seconds_counts_down_from_window_start(self):
        t = dtd.PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=1_000 + 3600),
                          self.WINDOW_SECONDS - 3600)

    def test_resets_in_seconds_floors_at_zero_past_the_boundary(self):
        t = dtd.PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=self.WINDOW_SECONDS + 500), 0)

    def test_state_round_trips_into_a_new_tracker(self):
        t = dtd.PlanWindowTracker(ceiling=100_000, window_start=500, window_total=20_000)
        restored = dtd.PlanWindowTracker(ceiling=100_000, **t.state())
        self.assertEqual(restored.window_start, 500)
        self.assertEqual(restored.window_total, 20_000)


class NotifyStatePersistenceTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(dtd.load_notify_state("/no/such/path.json"), {})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            state = {"sess-1": {"session_total": 12_345, "task_total": 0,
                                 "last_context_usage": 12_345}}
            dtd.save_notify_state(path, state)
            self.assertEqual(dtd.load_notify_state(path), state)


class NotificationWatcherTests(unittest.TestCase):
    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(dtd.json.dumps(obj) + "\n")

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
            scanner = dtd.Scanner(root)
            notify = dict(dtd.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = dtd.NotificationWatcher(
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
            notify = dict(dtd.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = dtd.Scanner(root)
            w1 = dtd.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline poll: session_total becomes 5000

            # simulate a dashboard restart: a brand-new watcher and Scanner,
            # same on-disk logs and same persisted state file
            scanner2 = dtd.Scanner(root)
            w2 = dtd.NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            w2.poll_once()

            # no new entries since the restart, so w2 shouldn't need to
            # touch this session at all — its persisted total must survive
            # unchanged (not doubled) in the state file it re-saves
            saved = dtd.load_notify_state(state_path)
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
            notify = dict(dtd.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = dtd.Scanner(root)
            w1 = dtd.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # sid becomes known, session_total=5000

            # this poll only looks at files modified in the last 0 minutes,
            # so sid's file is now "outside the active window" and won't be
            # touched — its persisted total must not be lost
            scanner2 = dtd.Scanner(root)
            w2 = dtd.NotificationWatcher(
                scanner2, notify=dict(notify, active_window_min=0),
                state_path=state_path)
            w2.poll_once()

            saved = dtd.load_notify_state(state_path)
            self.assertEqual(saved["sessions"][sid]["session_total"], 5_000)

    @patch.object(dtd, "send_windows_toast")
    def test_new_usage_after_baseline_fires_normally(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-1"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            scanner = dtd.Scanner(root)
            notify = dict(dtd.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = dtd.NotificationWatcher(
                scanner, notify=notify,
                state_path=os.path.join(root, "state.json"))
            watcher.poll_once()
            self.assertEqual(watcher.recent_events, [])  # baseline poll

            with open(path, "a", encoding="utf-8") as f:
                f.write(dtd.json.dumps(self._usage_entry(sid, 6_000, 0)) + "\n")
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
                f.write(dtd.json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, input_tokens, output_tokens, ts):
        return {"type": "assistant", "sessionId": sid, "timestamp": ts,
                "message": {"usage": {"input_tokens": input_tokens,
                                       "output_tokens": output_tokens,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}}}

    @patch.object(dtd, "send_windows_toast")
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
            notify = dict(dtd.NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")
            scanner = dtd.Scanner(root)
            watcher = dtd.NotificationWatcher(scanner, notify=notify, state_path=state_path)
            watcher.poll_once()  # baseline: both sessions newly discovered, silent
            self.assertEqual(watcher.recent_events, [])

            # new usage lands on both sessions between polls; combined they
            # cross 25/50/75% of the ceiling in one poll -> one toast, not three
            with open(path_a, "a", encoding="utf-8") as f:
                f.write(dtd.json.dumps(self._usage_entry(
                    "sess-a", 4_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            with open(path_b, "a", encoding="utf-8") as f:
                f.write(dtd.json.dumps(self._usage_entry(
                    "sess-b", 4_000, 0, "2026-01-01T00:01:01Z")) + "\n")
            watcher.poll_once()

            plan_events = [e for e in watcher.recent_events
                           if e["title"].startswith("Plan usage")]
            self.assertEqual(len(plan_events), 1, watcher.recent_events)
            self.assertIn("75%", plan_events[0]["message"])
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(dtd, "send_windows_toast")
    def test_plan_window_state_persists_across_restart(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(
                "sess-1", 1_000, 0, "2026-01-01T00:00:00Z")])
            notify = dict(dtd.NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")

            scanner1 = dtd.Scanner(root)
            w1 = dtd.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline: sess-1 newly discovered, plan window untouched

            with open(path, "a", encoding="utf-8") as f:
                f.write(dtd.json.dumps(self._usage_entry(
                    "sess-1", 3_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            w1.poll_once()  # sess-1 now known -> 3000 tokens added to the plan window

            saved = dtd.load_notify_state(state_path)
            self.assertEqual(saved["plan_window"]["window_total"], 3_000)

            # simulate restart: fresh watcher, same on-disk state
            scanner2 = dtd.Scanner(root)
            w2 = dtd.NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            self.assertEqual(w2.plan_tracker.window_total, 3_000)


class LoadRateLimitsTests(unittest.TestCase):
    """load_rate_limits() reads the file the statusLine hook writes -- see
    ~/.claude/statusline.js -- and must treat a stale or missing file as
    unavailable rather than serve a number that's stopped moving."""

    def test_missing_file_returns_none(self):
        self.assertIsNone(dtd.load_rate_limits("/no/such/path.json"))

    def test_fresh_file_returns_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            dtd.save_notify_state(path, data)
            self.assertEqual(dtd.load_rate_limits(path, now=1000.0 + 60), data)

    def test_stale_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            dtd.save_notify_state(path, data)
            stale_now = 1000.0 + dtd.RATE_LIMITS_STALE_SECONDS + 1
            self.assertIsNone(dtd.load_rate_limits(path, now=stale_now))

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertIsNone(dtd.load_rate_limits(path))


class OfficialRateLimitNotificationTests(unittest.TestCase):
    """The official-source path (poll_once -> _poll_official_rate_limits and
    plan_window_snapshot) must prefer real Anthropic numbers over the local
    estimate whenever a fresh statusline capture exists, and must not refire
    milestones already seen before a restart."""

    def _watcher(self, root, rate_limits_path):
        scanner = dtd.Scanner(root)
        return dtd.NotificationWatcher(
            scanner, notify=dict(dtd.NOTIFY, active_window_min=999_999),
            state_path=os.path.join(root, "state.json"),
            rate_limits_path=rate_limits_path)

    def _write_rl(self, path, five_pct, seven_pct=None, captured_at=None):
        rl = {"five_hour": {"used_percentage": five_pct, "resets_at": 9_999_999_999}}
        if seven_pct is not None:
            rl["seven_day"] = {"used_percentage": seven_pct, "resets_at": 9_999_999_999}
        dtd.save_notify_state(path, {
            "rate_limits": rl,
            "captured_at": captured_at if captured_at is not None else dtd.time.time(),
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
            self._write_rl(rl_path, 81, seven_pct=11, captured_at=dtd.time.time() - 300)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["captured_age_min"], 5)

    @patch.object(dtd, "send_windows_toast")
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

    @patch.object(dtd, "send_windows_toast")
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

    @patch.object(dtd, "send_windows_toast")
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

    def test_snapshot_falls_back_to_estimate_when_stale_or_missing(self):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")  # never written -> missing
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "estimate")

    @patch.object(dtd, "send_windows_toast")
    def test_first_sighting_sets_baseline_without_firing(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 60)
            w = self._watcher(root, rl_path)
            w.poll_once()
            self.assertEqual(w.recent_events, [])
            mock_toast.assert_not_called()

    @patch.object(dtd, "send_windows_toast")
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

    @patch.object(dtd, "send_windows_toast")
    def test_restart_does_not_refire_already_crossed_milestone(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            state_path = os.path.join(root, "state.json")
            self._write_rl(rl_path, 60)
            scanner1 = dtd.Scanner(root)
            w1 = dtd.NotificationWatcher(
                scanner1, notify=dict(dtd.NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w1.poll_once()  # baseline at 60%

            self._write_rl(rl_path, 76)  # crosses 75%
            w1.poll_once()
            self.assertEqual(mock_toast.call_count, 1)

            # restart: fresh watcher loads the persisted official_prev=76,
            # same 76% value on disk -> no new crossing, no refire
            scanner2 = dtd.Scanner(root)
            w2 = dtd.NotificationWatcher(
                scanner2, notify=dict(dtd.NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w2.poll_once()
            self.assertEqual(w2.recent_events, [])
            self.assertEqual(mock_toast.call_count, 1)


class StatuslineConfigTests(unittest.TestCase):
    def test_classify_missing_when_no_statusline_key(self):
        self.assertEqual(dtd.classify_statusline({}), "missing")

    def test_classify_missing_when_statusline_not_a_dict(self):
        self.assertEqual(dtd.classify_statusline({"statusLine": "oops"}), "missing")

    def test_classify_missing_when_command_absent(self):
        self.assertEqual(dtd.classify_statusline({"statusLine": {"type": "command"}}), "missing")

    def test_classify_ours_when_marker_in_command(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "C:\\Users\\x\\.claude\\dev_token_dashboard_statusline.js"'}}
        self.assertEqual(dtd.classify_statusline(settings), "ours")

    def test_classify_foreign_when_other_command(self):
        settings = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
        self.assertEqual(dtd.classify_statusline(settings), "foreign")

    def test_classify_missing_when_command_not_a_string(self):
        # A non-string command (e.g. an int, from a hand-edited settings.json)
        # must not raise TypeError from `in` on a non-string. It's treated as
        # "missing" (no usable command string present), consistent with the
        # existing convention that any malformed shape -- statusLine not a
        # dict, command key absent -- also classifies as "missing" rather
        # than "foreign".
        settings = {"statusLine": {"type": "command", "command": 12345}}
        self.assertEqual(dtd.classify_statusline(settings), "missing")

    def test_merge_adds_full_block_when_missing(self):
        new = dtd.merge_statusline_config({}, "/home/x/.claude/dev_token_dashboard_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command",
            "command": 'node "/home/x/.claude/dev_token_dashboard_statusline.js"',
            "refreshInterval": 30,
        })

    def test_merge_preserves_other_top_level_keys(self):
        settings = {"model": "opusplan", "enabledPlugins": {"foo": True}}
        new = dtd.merge_statusline_config(settings, "/x/dev_token_dashboard_statusline.js")
        self.assertEqual(new["model"], "opusplan")
        self.assertEqual(new["enabledPlugins"], {"foo": True})

    def test_merge_patches_refresh_interval_only_when_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'},
                    "model": "opusplan"}
        new = dtd.merge_statusline_config(settings, "/x/dev_token_dashboard_statusline.js")
        self.assertEqual(new["statusLine"]["refreshInterval"], 30)
        self.assertEqual(new["statusLine"]["command"], settings["statusLine"]["command"])
        self.assertEqual(new["model"], "opusplan")

    def test_merge_is_a_noop_when_already_correct(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"',
                                    "refreshInterval": 30}}
        new = dtd.merge_statusline_config(settings, "/x/dev_token_dashboard_statusline.js")
        self.assertEqual(new, settings)


class LoadSettingsJsonTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict_no_error(self):
        settings, err = dtd.load_settings_json("/no/such/settings.json")
        self.assertEqual(settings, {})
        self.assertIsNone(err)

    def test_valid_json_returns_parsed_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"model": "opusplan"}')
            settings, err = dtd.load_settings_json(path)
            self.assertEqual(settings, {"model": "opusplan"})
            self.assertIsNone(err)

    def test_malformed_json_returns_error_not_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            settings, err = dtd.load_settings_json(path)
            self.assertIsNone(settings)
            self.assertIsNotNone(err)


class ClaudeConfigDirTests(unittest.TestCase):
    def test_uses_env_var_when_set(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/custom/dir"}):
            self.assertEqual(dtd.claude_config_dir(), "/custom/dir")

    def test_falls_back_to_home_claude(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            expected = os.path.join(os.path.expanduser("~"), ".claude")
            self.assertEqual(dtd.claude_config_dir(), expected)


class NodeAvailableTests(unittest.TestCase):
    def test_true_when_which_finds_node(self):
        with patch.object(dtd.shutil, "which", return_value=r"C:\nodejs\node.exe"):
            self.assertTrue(dtd.node_available())

    def test_false_when_which_finds_nothing(self):
        with patch.object(dtd.shutil, "which", return_value=None):
            self.assertFalse(dtd.node_available())


class WriteSettingsWithBackupTests(unittest.TestCase):
    def test_no_backup_when_file_did_not_exist(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            backup = dtd.write_settings_with_backup(path, {"a": 1})
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
            backup = dtd.write_settings_with_backup(path, {"new": True})
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
            dtd.write_settings_with_backup(path, {"statusLine": {"type": "command"}})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"statusLine": {"type": "command"}})


class RunSetupNotificationsTests(unittest.TestCase):
    def _run(self, claude_dir, **kwargs):
        prints = []
        kwargs.setdefault("print_fn", prints.append)
        kwargs.setdefault("isatty_fn", lambda: True)
        code = dtd.run_setup_notifications(claude_dir=claude_dir, **kwargs)
        return code, prints

    @patch.object(dtd, "node_available", return_value=True)
    def test_dry_run_writes_nothing(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, dry_run=True, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertFalse(os.path.exists(os.path.join(d, dtd.STATUSLINE_MARKER)))
            self.assertTrue(any("Dry run" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_missing_statusline_writes_after_yes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 0)
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, dtd.STATUSLINE_MARKER)
            self.assertTrue(os.path.exists(settings_path))
            self.assertTrue(os.path.exists(js_path))
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
            self.assertIn(dtd.STATUSLINE_MARKER, settings["statusLine"]["command"])
            self.assertEqual(settings["statusLine"]["refreshInterval"], 30)

    @patch.object(dtd, "node_available", return_value=True)
    def test_missing_statusline_declines_on_no(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "n")
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("Cancelled" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_ours_already_correct_reports_no_changes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, dtd.STATUSLINE_MARKER)
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

    @patch.object(dtd, "node_available", return_value=True)
    def test_ours_correct_but_script_file_missing_falls_through_to_write(self, _node):
        # Regression test for Finding 1: settings already match (refreshInterval
        # 30, "ours" command) but the .js file itself is missing from disk --
        # e.g. deleted, or a settings.json that arrived on a second device
        # without its companion script. The wizard must NOT claim "Already
        # configured" (the hook is actually broken -- node has nothing to run)
        # and must instead proceed to the write flow so it can be repaired.
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, dtd.STATUSLINE_MARKER)
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

    @patch.object(dtd, "node_available", return_value=False)
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

    @patch.object(dtd, "node_available", return_value=True)
    def test_non_interactive_without_dry_run_aborts(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, isatty_fn=lambda: False,
                                      input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("not running interactively" in p.lower() for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_final_report_shown_after_successful_write(self, _node):
        with tempfile.TemporaryDirectory() as d:
            _, prints = self._run(d, input_fn=lambda _: "y")
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))
            self.assertTrue(any("Toast notifications:" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    @patch.object(dtd, "write_settings_with_backup", side_effect=OSError("Permission denied"))
    def test_write_permission_error_reported_not_crashed(self, _write, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 1)
            self.assertTrue(any("permission denied" in p.lower() for p in prints))
            # the statusline script write happens before the settings write in
            # source order, so it may exist -- but settings.json must not, since
            # write_settings_with_backup is what raised before touching it
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))


class ShouldRunNotificationsTests(unittest.TestCase):
    def test_true_by_default(self):
        self.assertTrue(dtd.should_run_notifications(dict(dtd.NOTIFY, enabled=True)))

    def test_false_when_disabled(self):
        self.assertFalse(dtd.should_run_notifications(dict(dtd.NOTIFY, enabled=False)))

    def test_true_when_key_absent(self):
        notify = dict(dtd.NOTIFY)
        del notify["enabled"]
        self.assertTrue(dtd.should_run_notifications(notify))

    def test_defaults_to_module_level_notify(self):
        with patch.dict(dtd.NOTIFY, {"enabled": False}):
            self.assertFalse(dtd.should_run_notifications())


class ForeignStatuslineTests(unittest.TestCase):
    def test_extract_script_path_from_quoted_command(self):
        self.assertEqual(
            dtd._extract_script_path('node "C:\\Users\\x\\.claude\\statusline.js"'),
            "C:\\Users\\x\\.claude\\statusline.js")

    def test_extract_script_path_returns_none_when_unquoted(self):
        self.assertIsNone(dtd._extract_script_path("~/.claude/statusline.sh"))

    @patch.object(dtd, "node_available", return_value=True)
    def test_foreign_statusline_does_not_write_settings(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            original = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(original, f)
            prints = []
            code = dtd.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                input_fn=lambda _: self.fail("must not prompt"),
                                                isatty_fn=lambda: True)
            self.assertEqual(code, 0)
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), original)
            self.assertFalse(os.path.exists(os.path.join(d, dtd.STATUSLINE_MARKER)))
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_foreign_statusline_reports_fresh_capture_if_present(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}, f)
            rl_path = os.path.join(d, "rate_limits_latest.json")
            with open(rl_path, "w", encoding="utf-8") as f:
                json.dump({"rate_limits": {}, "captured_at": dtd.time.time()}, f)
            prints = []
            with patch.object(dtd, "RATE_LIMITS_PATH", rl_path):
                dtd.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                             input_fn=lambda _: self.fail("must not prompt"),
                                             isatty_fn=lambda: True)
            self.assertTrue(any("already has a fresh capture" in p for p in prints))

    @patch.object(dtd, "node_available", return_value=True)
    def test_foreign_statusline_catches_non_oserror_exceptions(self, _node):
        """Verify that non-OSError exceptions (e.g. ValueError from isfile) are caught."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": 'node "/bad/path"'}}, f)
            prints = []
            # Mock os.path.isfile to raise ValueError (simulating a path with null bytes)
            with patch.object(dtd.os.path, "isfile", side_effect=ValueError("null byte in path")):
                code = dtd.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                    input_fn=lambda _: self.fail("must not prompt"),
                                                    isatty_fn=lambda: True)
            # Should complete without raising the ValueError
            self.assertEqual(code, 0)
            # Should still print the existing statusline message and final report
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))


class StartupHintTests(unittest.TestCase):
    def test_none_on_non_windows_even_if_missing(self):
        self.assertIsNone(dtd.startup_hint("/no/such/path.json", is_windows=False))

    def test_hint_when_windows_and_never_captured(self):
        hint = dtd.startup_hint("/no/such/path.json", is_windows=True)
        self.assertIsNotNone(hint)
        self.assertIn("--setup-notifications", hint)

    def test_none_when_file_already_exists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rate_limits_latest.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertIsNone(dtd.startup_hint(path, is_windows=True))


if __name__ == "__main__":
    unittest.main()
