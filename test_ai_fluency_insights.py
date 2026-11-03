"""Tests for ai_fluency_insights.py: notifications and setup, plus the My Adoption,
Flow, Quality, Value, Advisor and Data Health engines.

Run with:  python -m unittest test_ai_fluency_insights -v
"""
import json
import os
import shutil
import subprocess
import time
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

import ai_fluency_insights as afi


class MilestonesCrossedTests(unittest.TestCase):
    def test_single_milestone_crossed(self):
        # 50000/200000 = 25%
        self.assertEqual(afi.milestones_crossed(0, 50_000, 200_000), [25])

    def test_no_milestone_crossed_when_still_below(self):
        self.assertEqual(afi.milestones_crossed(0, 10_000, 200_000), [])

    def test_multiple_milestones_crossed_in_one_jump(self):
        self.assertEqual(afi.milestones_crossed(0, 200_000, 200_000),
                          [25, 50, 75, 85, 100])

    def test_already_past_milestone_does_not_refire(self):
        # prev is already above 50% threshold -> no new crossing
        self.assertEqual(afi.milestones_crossed(120_000, 130_000, 200_000), [])

    def test_zero_total_disables_milestones(self):
        self.assertEqual(afi.milestones_crossed(0, 999_999, 0), [])

    def test_drop_then_regrowth_refires_same_milestone(self):
        # simulates a context compaction: usage drops, then climbs back past 50%
        self.assertEqual(afi.milestones_crossed(90_000, 110_000, 200_000), [50])


class StepsCrossedTests(unittest.TestCase):
    def test_single_step_crossed(self):
        self.assertEqual(afi.steps_crossed(8_000, 12_000, 10_000), [10_000])

    def test_multiple_steps_crossed_in_one_jump(self):
        self.assertEqual(afi.steps_crossed(0, 25_000, 10_000),
                          [10_000, 20_000])

    def test_no_step_crossed_when_still_below(self):
        self.assertEqual(afi.steps_crossed(1_000, 4_999, 5_000), [])

    def test_no_step_crossed_on_no_increase(self):
        self.assertEqual(afi.steps_crossed(10_000, 10_000, 5_000), [])

    def test_zero_step_disables(self):
        self.assertEqual(afi.steps_crossed(0, 999_999, 0), [])


class WeightedTokensTests(unittest.TestCase):
    """Plan-window accounting must track quota-weighted usage, not a raw
    token sum — cache reads are ~0.1x the cost of fresh input (see PRICING),
    so a chat-heavy session with heavy tool-call caching would otherwise look
    like it burned far more quota than it actually did."""

    def test_input_and_output_weighted_by_pricing_ratio(self):
        # every PRICING tier prices output at 5x input, so output carries a
        # flat 5x weight regardless of model
        self.assertEqual(afi.weighted_tokens(1_000, 100, 0, 0), 1_000 + 500)

    def test_cache_write_weighted_at_1_25x(self):
        self.assertEqual(afi.weighted_tokens(0, 0, 1_000, 0), 1_250)

    def test_cache_read_weighted_at_0_1x(self):
        self.assertEqual(afi.weighted_tokens(0, 0, 0, 1_000), 100)


class SessionTokenTrackerTests(unittest.TestCase):
    def make_tracker(self, **notify_overrides):
        notify = dict(afi.NOTIFY)
        notify.update(notify_overrides)
        return afi.SessionTokenTracker(notify=notify)

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
        t = afi.SessionTokenTracker(notify=dict(afi.NOTIFY, context_window=0,
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
        t = afi.PlanWindowTracker(ceiling=100_000)
        events = t.add(ts_epoch=1_000, tokens=30_000)
        self.assertEqual(events, [25])
        self.assertEqual(t.window_start, 1_000)
        self.assertEqual(t.window_total, 30_000)

    def test_second_event_within_window_accumulates(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 30_000)
        events = t.add(1_000 + 3600, 30_000)  # 60% total now
        self.assertEqual(events, [50])
        self.assertEqual(t.window_total, 60_000)

    def test_event_at_exactly_5h_starts_a_new_window(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 90_000)
        events = t.add(self.WINDOW_SECONDS, 10_000)
        self.assertEqual(t.window_start, self.WINDOW_SECONDS)
        self.assertEqual(t.window_total, 10_000)
        self.assertEqual(events, [])  # 10% of the fresh window, no crossing

    def test_event_just_under_5h_stays_in_same_window(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        t.add(self.WINDOW_SECONDS - 1, 10_000)
        self.assertEqual(t.window_start, 0)
        self.assertEqual(t.window_total, 20_000)

    def test_zero_ceiling_disables_milestones(self):
        t = afi.PlanWindowTracker(ceiling=0)
        events = t.add(0, 50_000)
        self.assertEqual(events, [])

    def test_pct_caps_at_100(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 150_000)
        self.assertEqual(t.pct(now_epoch=100), 100)

    def test_pct_returns_zero_once_window_has_expired_without_a_new_event(self):
        # add() only resets window_start/window_total when a *new* usage
        # event arrives -- during an idle stretch past the 5h boundary,
        # nothing has called add() yet, so pct() must not keep reporting
        # the previous (now-expired) window's leftover percentage. This is
        # the exact "8% used, resets in 0h 0m" glitch seen in production:
        # resets_in_seconds() correctly says the window is over, but pct()
        # was still dividing the stale window_total by the ceiling.
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 80_000)
        expired_now = self.WINDOW_SECONDS + 500
        self.assertEqual(t.resets_in_seconds(expired_now), 0)
        self.assertEqual(t.pct(now_epoch=expired_now), 0)

    def test_resets_in_seconds_counts_down_from_window_start(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=1_000 + 3600),
                          self.WINDOW_SECONDS - 3600)

    def test_resets_in_seconds_floors_at_zero_past_the_boundary(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=self.WINDOW_SECONDS + 500), 0)

    def test_state_round_trips_into_a_new_tracker(self):
        t = afi.PlanWindowTracker(ceiling=100_000, window_start=500, window_total=20_000)
        restored = afi.PlanWindowTracker(ceiling=100_000, **t.state())
        self.assertEqual(restored.window_start, 500)
        self.assertEqual(restored.window_total, 20_000)


class NotifyStatePersistenceTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(afi.load_notify_state("/no/such/path.json"), {})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            state = {"sess-1": {"session_total": 12_345, "task_total": 0,
                                 "last_context_usage": 12_345}}
            afi.save_notify_state(path, state)
            self.assertEqual(afi.load_notify_state(path), state)


class NotificationWatcherTests(unittest.TestCase):
    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(afi.json.dumps(obj) + "\n")

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
            scanner = afi.Scanner(root)
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = afi.NotificationWatcher(
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
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline poll: session_total becomes 5000

            # simulate a dashboard restart: a brand-new watcher and Scanner,
            # same on-disk logs and same persisted state file
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            w2.poll_once()

            # no new entries since the restart, so w2 shouldn't need to
            # touch this session at all — its persisted total must survive
            # unchanged (not doubled) in the state file it re-saves
            saved = afi.load_notify_state(state_path)
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
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # sid becomes known, session_total=5000

            # this poll only looks at files modified in the last 0 minutes,
            # so sid's file is now "outside the active window" and won't be
            # touched — its persisted total must not be lost
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(
                scanner2, notify=dict(notify, active_window_min=0),
                state_path=state_path)
            w2.poll_once()

            saved = afi.load_notify_state(state_path)
            self.assertEqual(saved["sessions"][sid]["session_total"], 5_000)

    @patch.object(afi, "send_windows_toast")
    def test_new_usage_after_baseline_fires_normally(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-1"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            scanner = afi.Scanner(root)
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = afi.NotificationWatcher(
                scanner, notify=notify,
                state_path=os.path.join(root, "state.json"))
            watcher.poll_once()
            self.assertEqual(watcher.recent_events, [])  # baseline poll

            with open(path, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(sid, 6_000, 0)) + "\n")
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
                f.write(afi.json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, input_tokens, output_tokens, ts):
        return {"type": "assistant", "sessionId": sid, "timestamp": ts,
                "message": {"usage": {"input_tokens": input_tokens,
                                       "output_tokens": output_tokens,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}}}

    @patch.object(afi, "send_windows_toast")
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
            notify = dict(afi.NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")
            scanner = afi.Scanner(root)
            watcher = afi.NotificationWatcher(scanner, notify=notify, state_path=state_path)
            watcher.poll_once()  # baseline: both sessions newly discovered, silent
            self.assertEqual(watcher.recent_events, [])

            # new usage lands on both sessions between polls; combined they
            # cross 25/50/75% of the ceiling in one poll -> one toast, not three
            with open(path_a, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(
                    "sess-a", 4_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            with open(path_b, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(
                    "sess-b", 4_000, 0, "2026-01-01T00:01:01Z")) + "\n")
            watcher.poll_once()

            plan_events = [e for e in watcher.recent_events
                           if e["title"].startswith("Plan usage")]
            self.assertEqual(len(plan_events), 1, watcher.recent_events)
            self.assertIn("75%", plan_events[0]["message"])
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(afi, "send_windows_toast")
    def test_plan_window_state_persists_across_restart(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(
                "sess-1", 1_000, 0, "2026-01-01T00:00:00Z")])
            notify = dict(afi.NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")

            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline: sess-1 newly discovered, plan window untouched

            with open(path, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(
                    "sess-1", 3_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            w1.poll_once()  # sess-1 now known -> 3000 tokens added to the plan window

            saved = afi.load_notify_state(state_path)
            self.assertEqual(saved["plan_window"]["window_total"], 3_000)

            # simulate restart: fresh watcher, same on-disk state
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            self.assertEqual(w2.plan_tracker.window_total, 3_000)


class LoadRateLimitsTests(unittest.TestCase):
    """load_rate_limits() reads the file the statusLine hook writes -- see
    ~/.claude/statusline.js -- and must treat a stale or missing file as
    unavailable rather than serve a number that's stopped moving."""

    def test_missing_file_returns_none(self):
        self.assertIsNone(afi.load_rate_limits("/no/such/path.json"))

    def test_fresh_file_returns_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            afi.save_notify_state(path, data)
            self.assertEqual(afi.load_rate_limits(path, now=1000.0 + 60), data)

    def test_stale_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            afi.save_notify_state(path, data)
            stale_now = 1000.0 + afi.RATE_LIMITS_STALE_SECONDS + 1
            self.assertIsNone(afi.load_rate_limits(path, now=stale_now))

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertIsNone(afi.load_rate_limits(path))


class OfficialRateLimitNotificationTests(unittest.TestCase):
    """The official-source path (poll_once -> _poll_official_rate_limits and
    plan_window_snapshot) must prefer real Anthropic numbers over the local
    estimate whenever a fresh statusline capture exists, and must not refire
    milestones already seen before a restart."""

    def _watcher(self, root, rate_limits_path):
        scanner = afi.Scanner(root)
        return afi.NotificationWatcher(
            scanner, notify=dict(afi.NOTIFY, active_window_min=999_999),
            state_path=os.path.join(root, "state.json"),
            rate_limits_path=rate_limits_path)

    def _write_rl(self, path, five_pct, seven_pct=None, captured_at=None):
        rl = {"five_hour": {"used_percentage": five_pct, "resets_at": 9_999_999_999}}
        if seven_pct is not None:
            rl["seven_day"] = {"used_percentage": seven_pct, "resets_at": 9_999_999_999}
        afi.save_notify_state(path, {
            "rate_limits": rl,
            "captured_at": captured_at if captured_at is not None else afi.time.time(),
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
            self._write_rl(rl_path, 81, seven_pct=11, captured_at=afi.time.time() - 300)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["captured_age_min"], 5)

    @patch.object(afi, "send_windows_toast")
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

    @patch.object(afi, "send_windows_toast")
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

    @patch.object(afi, "send_windows_toast")
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
            stale_at = afi.time.time() - afi.RATE_LIMITS_STALE_SECONDS - 3600
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
            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 91,
                                               "resets_at": afi.time.time() - 60}},
                "captured_at": afi.time.time() - afi.RATE_LIMITS_STALE_SECONDS - 3600,
            })
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "official")
            self.assertEqual(snap["pct"], 0)

    @patch.object(afi, "send_windows_toast")
    def test_first_sighting_sets_baseline_without_firing(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 60)
            w = self._watcher(root, rl_path)
            w.poll_once()
            self.assertEqual(w.recent_events, [])
            mock_toast.assert_not_called()

    @patch.object(afi, "send_windows_toast")
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

    @patch.object(afi, "send_windows_toast")
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

    @patch.object(afi, "send_windows_toast")
    def test_new_window_refires_milestones_from_zero(self, mock_toast):
        # A genuinely new 5h window (different resets_at) must be free to
        # fire 25/50/75/85 again from scratch, even though the previous
        # window already fired them all.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            resets1 = afi.time.time() + 1000
            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": resets1}},
                "captured_at": afi.time.time()})
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 80%

            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 87, "resets_at": resets1}},
                "captured_at": afi.time.time()})
            w.poll_once()  # crosses 85% in window 1
            self.assertEqual(mock_toast.call_count, 1)

            resets2 = resets1 + 5 * 3600
            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": resets2}},
                "captured_at": afi.time.time()})
            w.poll_once()  # new window: crosses 25%
            self.assertEqual(mock_toast.call_count, 2)

            official_events = [e for e in w.recent_events if e["id"] == "official:five_hour:25"]
            self.assertEqual(len(official_events), 1, w.recent_events)

    @patch.object(afi, "send_windows_toast")
    def test_restart_does_not_refire_already_crossed_milestone(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            state_path = os.path.join(root, "state.json")
            self._write_rl(rl_path, 60)
            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(
                scanner1, notify=dict(afi.NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w1.poll_once()  # baseline at 60%

            self._write_rl(rl_path, 76)  # crosses 75%
            w1.poll_once()
            self.assertEqual(mock_toast.call_count, 1)

            # restart: fresh watcher loads the persisted official_prev=76,
            # same 76% value on disk -> no new crossing, no refire
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(
                scanner2, notify=dict(afi.NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w2.poll_once()
            self.assertEqual(w2.recent_events, [])
            self.assertEqual(mock_toast.call_count, 1)


class StatuslineConfigTests(unittest.TestCase):
    def test_classify_missing_when_no_statusline_key(self):
        self.assertEqual(afi.classify_statusline({}), "missing")

    def test_classify_missing_when_statusline_not_a_dict(self):
        self.assertEqual(afi.classify_statusline({"statusLine": "oops"}), "missing")

    def test_classify_missing_when_command_absent(self):
        self.assertEqual(afi.classify_statusline({"statusLine": {"type": "command"}}), "missing")

    def test_classify_ours_when_marker_in_command(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "C:\\Users\\x\\.claude\\ai_fluency_insights_statusline.js"'}}
        self.assertEqual(afi.classify_statusline(settings), "ours")

    def test_classify_foreign_when_other_command(self):
        settings = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
        self.assertEqual(afi.classify_statusline(settings), "foreign")

    def test_classify_missing_when_command_not_a_string(self):
        # A non-string command (e.g. an int, from a hand-edited settings.json)
        # must not raise TypeError from `in` on a non-string. It's treated as
        # "missing" (no usable command string present), consistent with the
        # existing convention that any malformed shape -- statusLine not a
        # dict, command key absent -- also classifies as "missing" rather
        # than "foreign".
        settings = {"statusLine": {"type": "command", "command": 12345}}
        self.assertEqual(afi.classify_statusline(settings), "missing")

    def test_merge_adds_full_block_when_missing(self):
        new = afi.merge_statusline_config({}, "/home/x/.claude/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command",
            "command": 'node "/home/x/.claude/ai_fluency_insights_statusline.js"',
            "refreshInterval": 30,
        })

    def test_merge_preserves_other_top_level_keys(self):
        settings = {"model": "opusplan", "enabledPlugins": {"foo": True}}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["model"], "opusplan")
        self.assertEqual(new["enabledPlugins"], {"foo": True})

    def test_merge_patches_refresh_interval_only_when_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/ai_fluency_insights_statusline.js"'},
                    "model": "opusplan"}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"]["refreshInterval"], 30)
        self.assertEqual(new["statusLine"]["command"], settings["statusLine"]["command"])
        self.assertEqual(new["model"], "opusplan")

    def test_merge_is_a_noop_when_already_correct(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/ai_fluency_insights_statusline.js"',
                                    "refreshInterval": 30}}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new, settings)

    def test_classify_legacy_pre_rename_install_as_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'}}
        self.assertEqual(afi.classify_statusline(settings), "ours")

    def test_merge_migrates_legacy_install_to_new_script_path(self):
        settings = {"statusLine": {"type": "command", "padding": 2,
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'}}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command", "padding": 2, "refreshInterval": 30,
            "command": 'node "/x/ai_fluency_insights_statusline.js"',
        })


class LoadSettingsJsonTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict_no_error(self):
        settings, err = afi.load_settings_json("/no/such/settings.json")
        self.assertEqual(settings, {})
        self.assertIsNone(err)

    def test_valid_json_returns_parsed_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"model": "opusplan"}')
            settings, err = afi.load_settings_json(path)
            self.assertEqual(settings, {"model": "opusplan"})
            self.assertIsNone(err)

    def test_malformed_json_returns_error_not_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            settings, err = afi.load_settings_json(path)
            self.assertIsNone(settings)
            self.assertIsNotNone(err)


class ClaudeConfigDirTests(unittest.TestCase):
    def test_uses_env_var_when_set(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/custom/dir"}):
            self.assertEqual(afi.claude_config_dir(), "/custom/dir")

    def test_falls_back_to_home_claude(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            expected = os.path.join(os.path.expanduser("~"), ".claude")
            self.assertEqual(afi.claude_config_dir(), expected)


class NodeAvailableTests(unittest.TestCase):
    def test_true_when_which_finds_node(self):
        with patch.object(afi.shutil, "which", return_value=r"C:\nodejs\node.exe"):
            self.assertTrue(afi.node_available())

    def test_false_when_which_finds_nothing(self):
        with patch.object(afi.shutil, "which", return_value=None):
            self.assertFalse(afi.node_available())


class WriteSettingsWithBackupTests(unittest.TestCase):
    def test_no_backup_when_file_did_not_exist(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            backup = afi.write_settings_with_backup(path, {"a": 1})
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
            backup = afi.write_settings_with_backup(path, {"new": True})
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
            afi.write_settings_with_backup(path, {"statusLine": {"type": "command"}})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"statusLine": {"type": "command"}})


class RunSetupNotificationsTests(unittest.TestCase):
    def _run(self, claude_dir, **kwargs):
        prints = []
        kwargs.setdefault("print_fn", prints.append)
        kwargs.setdefault("isatty_fn", lambda: True)
        code = afi.run_setup_notifications(claude_dir=claude_dir, **kwargs)
        return code, prints

    @patch.object(afi, "node_available", return_value=True)
    def test_dry_run_writes_nothing(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, dry_run=True, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertFalse(os.path.exists(os.path.join(d, afi.STATUSLINE_MARKER)))
            self.assertTrue(any("Dry run" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_missing_statusline_writes_after_yes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 0)
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
            self.assertTrue(os.path.exists(settings_path))
            self.assertTrue(os.path.exists(js_path))
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
            self.assertIn(afi.STATUSLINE_MARKER, settings["statusLine"]["command"])
            self.assertEqual(settings["statusLine"]["refreshInterval"], 30)

    @patch.object(afi, "node_available", return_value=True)
    def test_missing_statusline_declines_on_no(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "n")
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("Cancelled" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_ours_already_correct_reports_no_changes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
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

    @patch.object(afi, "node_available", return_value=True)
    def test_ours_correct_but_script_file_missing_falls_through_to_write(self, _node):
        # Regression test for Finding 1: settings already match (refreshInterval
        # 30, "ours" command) but the .js file itself is missing from disk --
        # e.g. deleted, or a settings.json that arrived on a second device
        # without its companion script. The wizard must NOT claim "Already
        # configured" (the hook is actually broken -- node has nothing to run)
        # and must instead proceed to the write flow so it can be repaired.
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
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

    @patch.object(afi, "node_available", return_value=False)
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

    @patch.object(afi, "node_available", return_value=True)
    def test_non_interactive_without_dry_run_aborts(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, isatty_fn=lambda: False,
                                      input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("not running interactively" in p.lower() for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_final_report_shown_after_successful_write(self, _node):
        with tempfile.TemporaryDirectory() as d:
            _, prints = self._run(d, input_fn=lambda _: "y")
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))
            self.assertTrue(any("Toast notifications:" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    @patch.object(afi, "write_settings_with_backup", side_effect=OSError("Permission denied"))
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
        self.assertTrue(afi.should_run_notifications(dict(afi.NOTIFY, enabled=True)))

    def test_false_when_disabled(self):
        self.assertFalse(afi.should_run_notifications(dict(afi.NOTIFY, enabled=False)))

    def test_true_when_key_absent(self):
        notify = dict(afi.NOTIFY)
        del notify["enabled"]
        self.assertTrue(afi.should_run_notifications(notify))

    def test_defaults_to_module_level_notify(self):
        with patch.dict(afi.NOTIFY, {"enabled": False}):
            self.assertFalse(afi.should_run_notifications())


class ForeignStatuslineTests(unittest.TestCase):
    def test_extract_script_path_from_quoted_command(self):
        self.assertEqual(
            afi._extract_script_path('node "C:\\Users\\x\\.claude\\statusline.js"'),
            "C:\\Users\\x\\.claude\\statusline.js")

    def test_extract_script_path_returns_none_when_unquoted(self):
        self.assertIsNone(afi._extract_script_path("~/.claude/statusline.sh"))

    @patch.object(afi, "node_available", return_value=True)
    def test_foreign_statusline_does_not_write_settings(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            original = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(original, f)
            prints = []
            code = afi.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                input_fn=lambda _: self.fail("must not prompt"),
                                                isatty_fn=lambda: True)
            self.assertEqual(code, 0)
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), original)
            self.assertFalse(os.path.exists(os.path.join(d, afi.STATUSLINE_MARKER)))
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_foreign_statusline_reports_fresh_capture_if_present(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}, f)
            rl_path = os.path.join(d, "rate_limits_latest.json")
            with open(rl_path, "w", encoding="utf-8") as f:
                json.dump({"rate_limits": {}, "captured_at": afi.time.time()}, f)
            prints = []
            with patch.object(afi, "RATE_LIMITS_PATH", rl_path):
                afi.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                             input_fn=lambda _: self.fail("must not prompt"),
                                             isatty_fn=lambda: True)
            self.assertTrue(any("already has a fresh capture" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_foreign_statusline_catches_non_oserror_exceptions(self, _node):
        """Verify that non-OSError exceptions (e.g. ValueError from isfile) are caught."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": 'node "/bad/path"'}}, f)
            prints = []
            # Mock os.path.isfile to raise ValueError (simulating a path with null bytes)
            with patch.object(afi.os.path, "isfile", side_effect=ValueError("null byte in path")):
                code = afi.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                    input_fn=lambda _: self.fail("must not prompt"),
                                                    isatty_fn=lambda: True)
            # Should complete without raising the ValueError
            self.assertEqual(code, 0)
            # Should still print the existing statusline message and final report
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))


class StartupHintTests(unittest.TestCase):
    def test_none_on_non_windows_even_if_missing(self):
        self.assertIsNone(afi.startup_hint("/no/such/path.json", is_windows=False,
                                            statusline_js_path="/no/such/statusline.js"))

    def test_hint_when_windows_and_never_captured(self):
        hint = afi.startup_hint("/no/such/path.json", is_windows=True,
                                 statusline_js_path="/no/such/statusline.js")
        self.assertIsNotNone(hint)
        self.assertIn("--setup-notifications", hint)

    def test_none_when_file_already_exists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rate_limits_latest.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertIsNone(afi.startup_hint(path, is_windows=True,
                                                statusline_js_path="/no/such/statusline.js"))

    def test_none_when_statusline_script_already_exists(self):
        # right after a successful --setup-notifications run, rate_limits_latest.json
        # doesn't exist yet (it only appears after the next Claude Code message) --
        # but the managed script does, so the nudge would be confusing here and
        # must be suppressed.
        with tempfile.TemporaryDirectory() as d:
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
            with open(js_path, "w", encoding="utf-8") as f:
                f.write("// managed")
            self.assertIsNone(afi.startup_hint("/no/such/rate_limits.json", is_windows=True,
                                                statusline_js_path=js_path))


class PrintFinalReportTests(unittest.TestCase):
    def test_toast_on_when_enabled_and_windows(self):
        prints = []
        with patch.object(afi.os, "name", "nt"), \
             patch.dict(afi.NOTIFY, {"enabled": True}):
            afi._print_final_report(prints.append)
        self.assertTrue(any("Toast notifications: ON" in p for p in prints), prints)

    def test_toast_off_when_disabled_on_windows(self):
        prints = []
        with patch.object(afi.os, "name", "nt"), \
             patch.dict(afi.NOTIFY, {"enabled": False}):
            afi._print_final_report(prints.append)
        self.assertTrue(any(p == "Toast notifications: OFF"
                             '  (edit NOTIFY["enabled"] in the script to change)'
                             for p in prints), prints)

    def test_toast_off_windows_only_on_non_windows(self):
        prints = []
        with patch.object(afi.os, "name", "posix"), \
             patch.dict(afi.NOTIFY, {"enabled": True}):
            afi._print_final_report(prints.append)
        self.assertTrue(any("Toast notifications: OFF (Windows only)" in p for p in prints), prints)


class ReportForeignStatuslineSizeGuardTests(unittest.TestCase):
    @staticmethod
    def _settings_for(command):
        return {"statusLine": {"type": "command", "command": command}}

    def test_skips_reading_a_large_foreign_script(self):
        with tempfile.TemporaryDirectory() as d:
            big_path = os.path.join(d, "huge.js")
            with open(big_path, "wb") as f:
                # the string IS present, so a guard-less read would find it --
                # this proves the size guard is what skips it, not the content
                f.write(b"// references rate_limits\n")
                f.seek(1_000_001)
                f.write(b"\0")
            prints = []
            afi._report_foreign_statusline(
                self._settings_for(f'node "{big_path}"'), prints.append)
            self.assertFalse(any("references rate_limits" in p for p in prints), prints)

    def test_still_reads_a_small_foreign_script(self):
        with tempfile.TemporaryDirectory() as d:
            small_path = os.path.join(d, "small.js")
            with open(small_path, "w", encoding="utf-8") as f:
                f.write("// writes rate_limits somewhere")
            prints = []
            afi._report_foreign_statusline(
                self._settings_for(f'node "{small_path}"'), prints.append)
            self.assertTrue(any("references rate_limits" in p for p in prints), prints)


class BundledStatuslineJsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_bundled_js_is_syntactically_valid(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "statusline.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(afi.BUNDLED_STATUSLINE_JS)
            result = subprocess.run(["node", "--check", path],
                                     capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class PricingTests(unittest.TestCase):
    def test_sonnet_rate_matches_current_anthropic_pricing(self):
        # Sonnet 5 is $2/$10 per MTok, confirmed permanent (the planned
        # $3/$15 rise was cancelled) -- see the token-economics brainstorm.
        rates = afi.price_for("claude-sonnet-5")
        self.assertEqual((rates["in"], rates["out"]), (2.0, 10.0))


class ColdStartAndCacheHealthTests(unittest.TestCase):
    """totals.cold_start_pct and totals.cache_reset_pct -- see the
    token-economics brainstorm's 'cold-start tax' and 'healthy loop'
    concepts. Both are derived from per-session cost tracking in
    touch_session/parse_entry and aggregated in stats_to_json."""

    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(afi.json.dumps(obj) + "\n")

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
            scanner = afi.Scanner(root)
            st = scanner.build_stats()
            totals = afi.stats_to_json(st)["totals"]
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
            scanner = afi.Scanner(root)
            st = scanner.build_stats()
            totals = afi.stats_to_json(st)["totals"]
            # 1 of the 2 non-first calls was a reset -> 50%
            self.assertEqual(totals["cache_reset_pct"], 50.0)


class AdoptionTests(unittest.TestCase):
    """build_adoption(): the My Adoption tab's score, pillars, features and coach."""

    @staticmethod
    def _stamp(days_ago, hour=10, minute=0):
        t = (afi.datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)
             - afi.timedelta(days=days_ago))
        return t.astimezone(afi.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

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
                    f.write(afi.json.dumps(e) + "\n")
        return afi.build_adoption(afi.Scanner(root).build_stats())

    def test_no_data_is_not_ready(self):
        a = self._build([])
        self.assertFalse(a["ready"])
        self.assertEqual(a["active_days"], 0)

    def test_baseline_needed_below_min_active_days(self):
        a = self._build([self._session("a", 0), self._session("b", 1)])
        self.assertFalse(a["ready"])
        self.assertEqual(a["active_days"], 2)
        self.assertEqual(a["min_days"], afi.ADOPTION["min_active_days"])

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
        self.assertEqual(a["level"]["name"], afi.adoption_level(a["score"])["name"])
        # the overall score really is the weighted blend of the four pillars
        wt = afi.ADOPTION["weights"]
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
            self.assertEqual(afi.adoption_level(score)["name"], name, score)

    def test_points_to_next_level(self):
        lv = afi.adoption_level(65)
        self.assertEqual(lv["to_next"], 10)
        self.assertEqual(lv["next_name"], "Pro")
        self.assertEqual(afi.adoption_level(95)["to_next"], 0)

    def test_pro_needs_every_pillar_above_the_floor(self):
        strong = {"habit": 90, "range": 80, "craft": 85, "impact": 80}
        self.assertEqual(afi.adoption_level(80, strong)["name"], "Pro")
        weak = dict(strong, range=40)
        lv = afi.adoption_level(80, weak)
        self.assertEqual(lv["name"], "Expert")
        self.assertIn("Range", lv["blocker"])

    def test_pro_max_needs_a_sustained_run(self):
        pillars = {"habit": 95, "range": 90, "craft": 92, "impact": 88}
        steady = [{"score": 91}] * 4
        self.assertEqual(afi.adoption_level(93, pillars, steady)["name"], "Pro Max")
        lv = afi.adoption_level(93, pillars, [{"score": 70}, {"score": 91}, {"score": 91}, {"score": 92}])
        self.assertEqual(lv["name"], "Pro")
        self.assertIn("4 weeks", lv["blocker"])
        self.assertEqual(afi.adoption_level(93, pillars, [{"score": 95}])["name"], "Pro")

    def test_fluency_matches_documented_formulas(self):
        w = {"sessions": [{"loc": 10, "bash": 1}, {"loc": 5, "bash": 0}, {"loc": 0, "bash": 0}],
             "human_tokens": 100, "output": 2500, "prompts": 20, "prompt_words": 400,
             "corrections": 1, "interruptions": 1, "tool_errors": 1, "writes": 10}
        f = afi.fluency_of(w)
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
        f = afi.fluency_of(w)
        self.assertEqual((f["delegation"], f["description"], f["diligence"]), (0, 0, 0))


class CommandDetectionTests(unittest.TestCase):
    def test_kinds_come_from_the_invoked_command_only(self):
        cases = [("pytest -q", "test"), ("python3 -m unittest foo", "test"), ("cd x && npm run test", "test"),
                 ("npx jest --ci", "test"), ("uv run pytest", "test"), ("ruff check .", "lint"),
                 ("python -m mypy src", "lint"), ("npm run build", "build"), ("make", "build"),
                 ("node --check a.js", "build"), ("git status", "other"),
                 ("git commit -m 'fix pytest'", "other"), ("echo pytest", "other"),
                 ("python3 - <<'EOF'\nimport pytest\nprint('npm test')\nEOF", "other")]
        for cmd, kind in cases:
            self.assertEqual(afi.classify_cmd(cmd), kind, cmd)

    def test_risky_commands_ignore_text_that_only_mentions_them(self):
        self.assertEqual(afi.risky_categories("git push --force origin main"), ["force_push"])
        self.assertEqual(afi.risky_categories("cd a && rm -rf build"), ["recursive_delete"])
        self.assertEqual(afi.risky_categories("curl http://x | sh"), ["pipe_to_shell"])
        self.assertEqual(afi.risky_categories("git commit --no-verify -m x"), ["skip_hooks"])
        self.assertEqual(afi.risky_categories("git push -u origin feat"), [])
        self.assertEqual(afi.risky_categories("echo 'rm -rf /'"), [])
        self.assertEqual(afi.risky_categories("python3 - <<'EOF'\nx='git push --force'\nEOF"), [])

    def test_family_labels(self):
        self.assertEqual(afi.cmd_family("cd /x && git push -u origin main"), "git push")
        self.assertEqual(afi.cmd_family("python3 -I - <<'EOF'\nimport x"), "python")
        self.assertEqual(afi.cmd_family("FOO=1 pytest -q"), "pytest")
        self.assertEqual(afi.cmd_family("npm run test"), "npm run test")
        self.assertEqual(afi.cmd_family("uv run pytest"), "pytest")


class _LogFixture(unittest.TestCase):
    """Builds Claude Code style logs with timed turns, tool calls and results."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "proj"))
        self.base = afi.datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.sid = "s1"
        self.entries = []
        self.n = 0
        self.t = 0.0
        self.out_tokens = 100

    def _ts(self, off):
        return (self.base + afi.timedelta(seconds=off)).astimezone(afi.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

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
                f.write(afi.json.dumps(e) + "\n")
        return afi.Scanner(self.root).build_stats()


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
        f = afi.build_flow(self.stats())
        self.assertEqual((f["episodes"], f["stuck"], f["unrecovered"], f["recovered"]), (1, 1, 1, 0))
        self.assertEqual(f["recovery_pct"], 0)

    def test_flow_needs_five_turns_for_a_score(self):
        for i in range(4):
            self.prompt("task %d" % i)
            self.end_turn()
        self.assertIsNone(afi.build_flow(self.stats())["score"])
        self.prompt("task 5")
        self.end_turn()
        f = afi.build_flow(self.stats())
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
        q = afi.build_quality(self.stats())
        self.assertEqual(q["edited_sessions"], 1)
        self.assertEqual(q["verified_pct"], 100)

    def test_check_before_the_edit_does_not_count(self):
        self._session(edit_first=False)
        q = afi.build_quality(self.stats())
        self.assertEqual(q["verified_pct"], 0)

    def test_first_run_pass_and_fail_then_pass_recovery(self):
        self.prompt("fix")
        self.edit()
        self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.tool("Bash", {"command": "pytest"})
        self.end_turn()
        q = afi.build_quality(self.stats())
        self.assertEqual(q["first_run_pass_pct"], 0)
        self.assertEqual(q["runs"]["test"]["runs"], 2)
        self.assertEqual((q["runs"]["test"]["fail_groups"], q["runs"]["test"]["recovered_groups"]), (1, 1))

    def test_tests_alongside_counts_test_file_edits(self):
        self.prompt("add feature with tests")
        self.edit("/p/src/a.py")
        self.edit("/p/tests/test_a.py")
        self.end_turn()
        q = afi.build_quality(self.stats())
        self.assertEqual(q["tests_alongside_pct"], 100)

    def test_reedit_share_and_hotspot(self):
        self.prompt("iterate")
        for _ in range(6):
            self.edit("/p/src/hot.py")
        self.edit("/p/src/other.py")
        self.end_turn()
        q = afi.build_quality(self.stats())
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
        q = afi.build_quality(self.stats())
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
        v = afi.build_value(self.stats())
        self.assertEqual(v["loc"], 50)
        self.assertEqual(v["verified_sessions"], 1)
        self.assertGreater(v["cost_per_verified"], 0)
        self.assertAlmostEqual(v["per_100_loc"], v["cost"] / 50 * 100, places=2)


class LogResilienceTests(_LogFixture):
    """Phase 1 of docs/LOG_RESILIENCE_ROADMAP.md: findings F1-F5 from docs/SCHEMA_NOTES.md."""

    def _write(self, rel, entries, trailer="\n"):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(afi.json.dumps(e) for e in entries) + trailer)
        return path

    def _one_session(self):
        self.prompt("add pagination to the orders list")
        self.end_turn()
        return list(self.entries)

    # F1 ---------------------------------------------------------------
    def test_set_aside_copy_is_not_double_counted(self):
        ents = self._one_session()
        self._write("proj/s1.jsonl", ents)
        self._write("proj/s1.orphaned-1730000000-abc.jsonl", ents)
        st = afi.Scanner(self.root).build_stats()
        base = afi.Scanner(self._clone_without_copy(ents)).build_stats()
        self.assertEqual(sum(d["messages"] for d in st.days.values()),
                         sum(d["messages"] for d in base.days.values()))
        self.assertEqual(st.hc["set_aside_files"], 1)

    def _clone_without_copy(self, ents):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        p = os.path.join(other, "proj")
        os.makedirs(p)
        with open(os.path.join(p, "s1.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(afi.json.dumps(e) for e in ents) + "\n")
        return other

    # F2 ---------------------------------------------------------------
    def test_subagent_files_belong_to_the_parent_project(self):
        ents = self._one_session()
        self._write("-home-u-proj/s1.jsonl", ents)
        sub = [dict(e, isSidechain=True, uuid=str(e.get("uuid", "")) + "x") for e in ents]
        for e in sub:
            if "message" in e and isinstance(e["message"], dict) and e["message"].get("id"):
                e["message"] = dict(e["message"], id=e["message"]["id"] + "x")
        self._write("-home-u-proj/s1/subagents/agent-1.jsonl", sub)
        st = afi.Scanner(self.root).build_stats()
        self.assertNotIn("subagents", st.projects)
        self.assertEqual(list(st.projects), ["home/u/proj"])

    def test_project_label_layouts(self):
        r = os.path.join("x", "projects")
        self.assertEqual(afi.project_label(r, os.path.join(r, "-home-u-p", "s.jsonl")), "home/u/p")
        self.assertEqual(afi.project_label(r, os.path.join(r, "-home-u-p", "s", "subagents", "a.jsonl")), "home/u/p")
        self.assertEqual(afi.project_label(r, os.path.join(r, "s.jsonl")), "projects")

    # F4 ---------------------------------------------------------------
    def test_decode_errors_split_into_torn_tail_and_mid_file(self):
        ents = self._one_session()
        path = os.path.join(self.root, "proj", "s1.jsonl")
        lines = [afi.json.dumps(e) for e in ents]
        text = "\n".join([lines[0], "{broken mid line", lines[1]]) + "\n" + '{"type": "assi'
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        st = afi.Scanner(self.root).build_stats()
        self.assertEqual(st.hc["decode_errors_mid"], 1)
        self.assertEqual(st.hc["decode_errors_torn"], 1)
        self.assertEqual(st.parse_errors, 1)        # a torn tail is expected while a session is live
        caps = {c["id"]: c for c in afi.build_health(st)["capabilities"]}
        self.assertEqual(caps["readable"]["status"], "partial")

    def test_clean_files_report_readable(self):
        self._one_session()
        caps = {c["id"]: c for c in afi.build_health(self.stats())["capabilities"]}
        self.assertEqual(caps["readable"]["status"], "ok")

    # F3 ---------------------------------------------------------------
    def test_tokens_check_can_fail_when_usage_fields_are_renamed(self):
        self.prompt("hello there")
        self.end_turn()
        for e in self.entries:
            u = e.get("message", {}).get("usage") if isinstance(e.get("message"), dict) else None
            if u is not None:
                u["cache_read_tokens"] = u.pop("cache_read_input_tokens")      # simulated rename
        caps = {c["id"]: c for c in afi.build_health(self.stats())["capabilities"]}
        self.assertNotEqual(caps["tokens"]["status"], "ok")
        self.assertIn("cache_read_input_tokens", caps["tokens"]["detail"])

    def test_tokens_check_ok_on_normal_logs(self):
        self.prompt("hello there")
        self.end_turn()
        caps = {c["id"]: c for c in afi.build_health(self.stats())["capabilities"]}
        self.assertEqual(caps["tokens"]["status"], "ok")

    # F5 ---------------------------------------------------------------
    def test_entry_types_are_classified(self):
        self._one_session()
        self.entries.append({"type": "queue-operation", "operation": "enqueue", "sessionId": "s1"})
        self.entries.append({"type": "brand-new-thing", "sessionId": "s1", "x": 1})
        h = afi.build_health(self.stats())
        status = {t["type"]: t["status"] for t in h["entry_types"]}
        self.assertEqual(status["assistant"], "handled")
        self.assertEqual(status["queue-operation"], "ignored")
        self.assertEqual(status["brand-new-thing"], "new")
        self.assertEqual([t["type"] for t in h["unknown_entry_types"]], ["brand-new-thing"])


class SchemaSentinelTests(_LogFixture):
    """The drift sentinel (docs/LOG_RESILIENCE_ROADMAP.md section 2), retention note and fixture redaction."""

    def _mk(self, i, ver="2.1.316", drop=(), **over):
        """One realistic assistant entry; `drop` removes dotted fields, `over` replaces top-level keys."""
        e = {"type": "assistant", "sessionId": "s%d" % (i // 5), "timestamp": self._ts(i), "version": ver,
             "uuid": "u%s%d" % (ver, i), "requestId": "r%s%d" % (ver, i),
             "message": {"id": "m%s%d" % (ver, i), "model": "claude-sonnet-5", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "x"}],
                         "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 100}}}
        e.update(over)
        for dotted in drop:
            *parents, leaf = dotted.split(".")
            node = e
            for part in parents:
                node = node[part]
            node.pop(leaf, None)
        return e

    def _analyze(self, entries, manifest=None, **kw):
        path = os.path.join(self.root, "proj", "a.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(afi.json.dumps(e) + "\n")
        st = afi.Scanner(self.root).build_stats()
        return afi.analyze_schema(st.census_paths, st.census_types, manifest, **kw), st

    def _kinds(self, res):
        return {(f["kind"], f["path"]) for f in res["findings"] if f["severity"] == "warning"}

    # --- flattening --------------------------------------------------------
    def test_flatten_collapses_ids_maps_and_opaque_subtrees(self):
        e = {"type": "x", "wireToolInputs": {"toolu_01ABC": {"a": 1}},
             "modelUsage": {"claude-new-9": {"costUSD": 1.5}},
             "u": {"123e4567-e89b-12d3-a456-426614174000": 1}, "m": [{"k": 1}, {"k": "s"}]}
        flat = afi.flatten_entry(e)
        paths = {p for p, _t in flat}
        self.assertIn("wireToolInputs", paths)
        self.assertFalse([p for p in paths if p.startswith("wireToolInputs.")])
        self.assertIn("modelUsage.<key>.costUSD", paths)
        self.assertIn("u.<id>", paths)
        self.assertTrue({("m[].k", "int"), ("m[].k", "str")} <= flat)

    # --- consumed-field drift ---------------------------------------------
    def test_healthy_logs_raise_no_warnings(self):
        res, _ = self._analyze([self._mk(i) for i in range(30)], afi.load_schema_manifest())
        self.assertEqual(res["summary"]["warning"], 0, res["findings"])

    def test_field_missing_everywhere_is_a_warning_and_degrades_metrics(self):
        res, _ = self._analyze([self._mk(i, drop=["message.usage.cache_read_input_tokens"]) for i in range(30)])
        self.assertIn(("missing_all", "message.usage.cache_read_input_tokens"), self._kinds(res))
        self.assertIn("cache_savings", res["affected_metrics"])
        cat = afi.catalog_payload(res)
        self.assertIn("cache read tokens", cat["cache_savings"]["degraded"])
        self.assertIsNone(cat["turn_time"]["degraded"])

    def test_every_tile_dependency_names_a_consumed_field(self):
        labels = {c[5] for c in afi.CONSUMED_FIELDS}
        for tile, fields in afi.TILE_FIELDS.items():
            self.assertTrue(fields, tile)
            for f in fields:
                self.assertIn(f, labels, "tile %r depends on unknown field %r" % (tile, f))

    def test_healthy_logs_mark_no_tile(self):
        res, _ = self._analyze([self._mk(i) for i in range(30)], afi.load_schema_manifest())
        self.assertEqual(res["affected_tiles"], {})

    def test_missing_field_marks_exactly_the_tiles_built_on_it(self):
        res, _ = self._analyze([self._mk(i, drop=["message.usage.cache_read_input_tokens"]) for i in range(30)])
        tiles = res["affected_tiles"]
        self.assertEqual(set(tiles), {"cache_read", "cost", "cold_start"})
        self.assertEqual(tiles["cache_read"], ["cache read tokens"])
        for untouched in ("input", "output", "prompts", "sessions", "loc"):
            self.assertNotIn(untouched, tiles)

    def test_retyped_field_marks_its_tiles(self):
        entries = [self._mk(i) for i in range(30)]
        for e in entries:
            e["message"]["usage"]["output_tokens"] = "12"
        res, _ = self._analyze(entries)
        self.assertIn("output", res["affected_tiles"])
        self.assertIn("leverage", res["affected_tiles"])

    def test_tile_flags_reach_the_page_payload(self):
        self.assertIn("affected_tiles", afi.analyze_schema({}, {}, None))

    def test_field_that_vanished_in_a_newer_version_names_the_version(self):
        entries = [self._mk(i, ver="2.1.0") for i in range(25)] + \
                  [self._mk(100 + i, ver="2.2.0", drop=["message.stop_reason"]) for i in range(25)]
        res, _ = self._analyze(entries)
        f = [f for f in res["findings"] if f["kind"] == "vanished"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["versions"], ["2.2.0"])
        self.assertIn("2.2.0", f[0]["title"])

    def test_type_change_is_a_warning(self):
        entries = [self._mk(i) for i in range(25)]
        for e in entries:
            e["message"]["usage"]["cache_read_input_tokens"] = "100"      # int became a string
        res, _ = self._analyze(entries)
        self.assertIn(("type_change", "message.usage.cache_read_input_tokens"), self._kinds(res))

    def test_too_little_evidence_is_not_drift(self):
        res, _ = self._analyze([self._mk(i, drop=["message.model"]) for i in range(5)])
        self.assertEqual(res["summary"]["warning"], 0)

    def test_optional_fields_may_be_absent(self):
        # thinkingDurationMs only appears on thinking turns; its absence must never be flagged
        res, _ = self._analyze([self._mk(i) for i in range(30)])
        self.assertNotIn(("missing_all", "thinkingDurationMs"), self._kinds(res))

    # --- baseline comparison ----------------------------------------------
    def test_new_fields_are_info_only_and_need_a_baseline(self):
        entries = [self._mk(i, brandNewField={"deep": 1}) for i in range(25)]
        manifest = afi.load_schema_manifest()
        res, _ = self._analyze(entries, manifest)
        new = [f for f in res["findings"] if f["kind"] == "new_field"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["severity"], "info")
        self.assertIn("brandNewField", new[0]["paths"])
        self.assertEqual(res["summary"]["warning"], 0)
        res2, _ = self._analyze(entries, None)            # no baseline: say so, report nothing about new fields
        self.assertFalse(res2["baseline"]["loaded"])
        self.assertFalse([f for f in res2["findings"] if f["kind"] == "new_field"])

    def test_shipped_baseline_covers_every_always_present_consumed_field(self):
        m = afi.load_schema_manifest()
        self.assertIsNotNone(m, "schema/claude_code_known.json is missing or unreadable")
        for et, path, _acc, always, _metrics, _label in afi.CONSUMED_FIELDS:
            if always:
                self.assertIn(path, m["entry_types"].get(et, {}), (et, path))

    def test_new_model_name_does_not_look_like_new_fields(self):
        m = afi.load_schema_manifest()
        e = {"type": "cost-state", "sessionId": "s", "modelUsage": {"claude-brand-new-9": {"costUSD": 1.0}}}
        paths = {p for p, _t in afi.flatten_entry(e)}
        self.assertTrue(paths <= set(m["entry_types"]["cost-state"]) | {"modelUsage.<key>.costUSD"}, paths)

    # --- report ------------------------------------------------------------
    def test_report_text(self):
        res, st = self._analyze([self._mk(i, drop=["message.model"]) for i in range(30)], afi.load_schema_manifest())
        text = afi.render_schema_report({"schema": res, "unknown_entry_types": []})
        self.assertIn("[WARNING]", text)
        self.assertIn("message.model", text)
        ok, _ = self._analyze([self._mk(i) for i in range(30)], afi.load_schema_manifest())
        self.assertIn("No log format changes detected", afi.render_schema_report({"schema": ok, "unknown_entry_types": []}))

    def test_health_payload_carries_schema_and_serialises(self):
        self.prompt("hello")
        self.end_turn()
        h = afi.build_health(self.stats())
        self.assertIn("schema", h)
        afi.json.dumps(h)

    # --- retention ---------------------------------------------------------
    def _config(self, content):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        if content is not None:
            with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else afi.json.dumps(content))
        return d

    def test_retention_reads_the_setting_and_rejects_bad_values(self):
        self.assertEqual(afi.load_retention(self._config({"cleanupPeriodDays": 90}))["days"], 90)
        for bad in (None, {}, {"cleanupPeriodDays": 0}, {"cleanupPeriodDays": True},
                    {"cleanupPeriodDays": "90"}, "{not json"):
            r = afi.load_retention(self._config(bad))
            self.assertEqual((r["days"], r["source"]), (30, "the Claude Code default"), bad)

    def test_history_window_capability(self):
        self.prompt("hello")
        self.end_turn()
        from unittest import mock
        for cfg, status in (({}, "partial"), ({"cleanupPeriodDays": 400}, "ok")):
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self._config(cfg)}):
                caps = {c["id"]: c for c in afi.build_health(self.stats())["capabilities"]}
            self.assertEqual(caps["history"]["status"], status)
        self.assertIn("older history is gone", caps["history"]["detail"])

    # --- redaction ---------------------------------------------------------
    SECRETS = ("hunter2", "alice", "corp.com", "internal.example", "secret-project", "mcp__corp__deploy", "S-secret",
               "toolu_01XYZ")

    def _secret_entry(self):
        return {"type": "assistant", "uuid": "u-1", "sessionId": "S-secret", "cwd": "/home/alice/secret-project",
                "timestamp": "2026-11-01T10:00:00.000Z", "version": "2.1.316", "isSidechain": False,
                "attachment": {"context": {"userEmail": "alice@corp.com"}},
                "wireToolInputs": {"toolu_01XYZ": {"command": "curl https://internal.example/token"}},
                "message": {"id": "m1", "model": "claude-sonnet-5", "stop_reason": "tool_use",
                            "content": [{"type": "text", "text": "my password is hunter2"},
                                        {"type": "tool_use", "id": "toolu_01XYZ", "name": "mcp__corp__deploy",
                                         "input": {"file_path": "/home/alice/x.py"}},
                                        {"type": "tool_use", "id": "toolu_02", "name": "Bash",
                                         "input": {"command": "ls"}}],
                            "usage": {"input_tokens": 12, "output_tokens": 34, "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 56}}}

    def test_redaction_removes_content_and_keeps_structure(self):
        ids = {}
        r1 = afi.redact_entry(self._secret_entry(), ids)
        text = afi.json.dumps(r1)
        for secret in self.SECRETS:
            self.assertNotIn(secret, text)
        self.assertEqual(r1["type"], "assistant")
        self.assertEqual(r1["message"]["model"], "claude-sonnet-5")
        self.assertEqual(r1["message"]["usage"]["output_tokens"], 34)
        self.assertEqual(r1["timestamp"], "2026-11-01T10:00:00.000Z")
        self.assertEqual(r1["message"]["content"][2]["name"], "Bash")
        self.assertEqual(afi.flatten_entry(r1), afi.flatten_entry(self._secret_entry()))   # same structure
        # ids are consistent pseudonyms across entries, so de-duplication still works
        r2 = afi.redact_entry(self._secret_entry(), ids)
        self.assertEqual(r1["sessionId"], r2["sessionId"])
        self.assertNotEqual(r1["sessionId"], r1["uuid"])

    def test_redacted_logs_give_the_same_token_totals(self):
        self.prompt("add pagination to the orders list please")
        self.edit()
        self.end_turn()
        a = self.stats()
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        os.makedirs(os.path.join(other, "proj"))
        ids = {}
        with open(os.path.join(other, "proj", "a.jsonl"), "w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(afi.json.dumps(afi.redact_entry(e, ids)) + "\n")
        b = afi.Scanner(other).build_stats()
        tot = lambda st, k: sum(d[k] for d in st.days.values())      # noqa: E731
        for k in ("input", "output", "cache_read", "cache_write"):
            self.assertEqual(tot(a, k), tot(b, k), k)
        self.assertEqual(len(a.turns), len(b.turns))
        self.assertEqual(a.parse_errors, b.parse_errors)


class IncrementalReadTests(_LogFixture):
    """Phase 2a: the scanner reads only the bytes appended since last time, and notices rewrites."""

    def _file(self, name="grow.jsonl"):
        return os.path.join(self.root, "proj", name)

    def _append(self, path, data):
        with open(path, "ab") as f:
            f.write(data)

    def _lines(self):
        self.prompt("add pagination to the orders list please")
        self.edit()
        self.prompt("now add a test for it")
        self.edit(path="/p/tests/test_a.py")
        self.end_turn()
        return [afi.json.dumps(e).encode() + b"\n" for e in self.entries]

    # --- read_tail -----------------------------------------------------
    def test_read_tail_returns_only_new_complete_lines(self):
        p = self._file()
        self._append(p, b'{"a":1}\n{"b":2}\n{"c":')                  # last line still being written
        lines, st, rew, torn = afi.read_tail(p, None)
        self.assertEqual((lines, rew, torn), ([b'{"a":1}', b'{"b":2}'], False, 1))
        self.assertEqual(st["offset"], len(b'{"a":1}\n{"b":2}\n'))
        self._append(p, b'3}\n{"d":4}\n')
        lines, st2, rew, torn = afi.read_tail(p, st)
        self.assertEqual((lines, rew, torn), ([b'{"c":3}', b'{"d":4}'], False, 0))

    def test_read_tail_consumes_a_complete_last_line_without_newline(self):
        p = self._file()
        self._append(p, b'{"a":1}\n{"b":2}')
        lines, st, _r, torn = afi.read_tail(p, None)
        self.assertEqual((lines, torn), ([b'{"a":1}', b'{"b":2}'], 0))
        self._append(p, b"\n{\"c\":3}\n")                              # newline arrives later, then more
        lines, _st, rew, _t = afi.read_tail(p, st)
        self.assertEqual((lines, rew), ([b'{"c":3}'], False))

    def test_read_tail_detects_truncation_and_rewrites(self):
        p = self._file()
        body = b"".join(b'{"n":%d,"pad":"%s"}\n' % (i, b"x" * 40) for i in range(40))
        self._append(p, body)
        _l, st, _r, _t = afi.read_tail(p, None)
        for label, new in (("truncated", body[:100]),
                           ("rewritten from the start", b"".join(b'{"n":%d,"pad":"%s"}\n' % (i, b"y" * 40) for i in range(60))),
                           ("edited just before the checkpoint", body[:-40] + b"Z" * 20 + body[-20:] + b'{"n":99}\n')):
            with open(p, "wb") as f:
                f.write(new)
            lines, _st, rew, _t = afi.read_tail(p, st)
            self.assertTrue(rew, label)
            self.assertEqual(len(lines), new.count(b"\n"), label)

    # --- Scanner ---------------------------------------------------------
    def _same_stats(self, a, b):
        self.assertEqual(dict(a.days), dict(b.days))
        self.assertEqual(sorted(a.sessions), sorted(b.sessions))
        self.assertEqual(len(a.turns), len(b.turns))
        self.assertEqual(a.parse_errors, b.parse_errors)
        self.assertEqual(dict(a.hc), dict(b.hc))
        self.assertEqual(a.census_paths, b.census_paths)
        self.assertEqual(a.census_types, b.census_types)

    def test_growing_file_matches_a_fresh_scan_and_reads_each_byte_once(self):
        data = b"".join(self._lines())
        p = self._file()
        sc = afi.Scanner(self.root)
        cuts = [0, 17, 300, 301, 900, len(data) - 400, len(data) - 5, len(data)]   # cut mid-line on purpose
        for a, b in zip(cuts, cuts[1:]):
            self._append(p, data[a:b])
            sc.build_stats()
        fresh = afi.Scanner(self.root).build_stats()
        self._same_stats(sc.build_stats(), fresh)
        self.assertEqual(sc.bytes_read, len(data))                               # no byte was read twice

    def test_rewritten_file_is_reread_not_double_counted(self):
        data = b"".join(self._lines())
        p = self._file()
        self._append(p, data)
        sc = afi.Scanner(self.root)
        sc.build_stats()
        with open(p, "wb") as f:                                                  # same session, rewritten shorter
            f.write(b"".join(data.splitlines(True)[:3]))
        self._same_stats(sc.build_stats(), afi.Scanner(self.root).build_stats())

    def test_unchanged_files_are_not_read_again(self):
        self._append(self._file(), b"".join(self._lines()))
        sc = afi.Scanner(self.root)
        sc.build_stats()
        n = sc.bytes_read
        sc.build_stats()
        sc.build_stats()
        self.assertEqual(sc.bytes_read, n)

    def test_mid_file_decode_errors_accumulate_across_increments(self):
        p = self._file()
        self._append(p, b'{"type":"user"}\n{broken\n')
        sc = afi.Scanner(self.root)
        self.assertEqual(sc.build_stats().hc["decode_errors_mid"], 1)
        self._append(p, b'{"type":"user"}\n{also broken\n{"type":"user"}\n')
        st = sc.build_stats()
        self.assertEqual(st.hc["decode_errors_mid"], 2)
        self.assertEqual(st.hc["decode_errors_torn"], 0)


class _ArchiveBase(_LogFixture):
    """Helpers shared by the archive test classes (no tests of its own)."""

    def setUp(self):
        super().setUp()
        self.adir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.adir, True)
        self.apath = os.path.join(self.adir, "state", "archive.sqlite")

    def _session(self, n=2):
        for i in range(n):
            self.prompt("add pagination to the orders list, round %d" % i)
            self.edit()
            self.end_turn()
        return b"".join(afi.json.dumps(e).encode() + b"\n" for e in self.entries)

    def _put(self, rel, data):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def _arc(self, enable=True, **kw):
        a = afi.Archive(self.apath, **kw)
        self.addCleanup(a.close)
        if enable:
            self.assertTrue(a.enable(), a.last_error)
        return a

    def _scan(self, arc, root=None):
        return afi.Scanner(root or self.root, archive=arc)

    def _totals(self, st):
        return (sum(d["output"] for d in st.days.values()), sum(d["messages"] for d in st.days.values()),
                len(st.sessions), sorted(st.projects), len(st.turns))


class ArchiveTests(_ArchiveBase):
    """Phase 2b: the opt-in local archive keeps history after Claude Code deletes its logs."""

    # --- consent -----------------------------------------------------------
    def test_nothing_is_written_until_enabled(self):
        self._put("proj/s1.jsonl", self._session())
        arc = self._arc(enable=False)
        self._scan(arc).build_stats()
        self.assertFalse(os.path.exists(self.apath))
        self.assertFalse(arc.enabled)
        self.assertFalse(os.path.exists(os.path.dirname(self.apath)))

    def test_enable_then_scan_backfills_existing_logs(self):
        data = self._session()
        self._put("proj/s1.jsonl", data)
        arc = self._arc()
        st = self._scan(arc).build_stats()
        s = arc.status()
        self.assertTrue(s["enabled"])
        self.assertEqual((s["sources"], s["lines"]), (1, data.count(b"\n")))
        self.assertEqual(st.archive["lines"], s["lines"])

    def test_disable_stops_capture_but_keeps_and_still_restores_data(self):
        data = self._session()
        p = self._put("proj/s1.jsonl", data)
        arc = self._arc()
        sc = self._scan(arc)
        before = self._totals(sc.build_stats())
        arc.disable()
        with open(p, "ab") as f:
            f.write(b'{"type":"user"}\n')
        sc.build_stats()
        self.assertEqual(arc.status()["lines"], data.count(b"\n"))              # nothing new captured
        os.remove(p)
        self.assertEqual(self._totals(self._scan(arc).build_stats()), before)    # old data still restores

    def test_purge_removes_the_files(self):
        self._put("proj/s1.jsonl", self._session())
        arc = self._arc()
        self._scan(arc).build_stats()
        arc.purge()
        self.assertFalse(os.path.exists(self.apath))
        self.assertFalse(arc.exists())
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.apath)) if f.startswith("archive")], [])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_archive_is_private_to_the_user(self):
        self._arc()
        self.assertEqual(os.stat(self.apath).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.apath)).st_mode & 0o777, 0o700)

    # --- ingest ------------------------------------------------------------
    def test_ingest_is_incremental_and_idempotent(self):
        data = self._session(3)
        cut = data.index(b"\n", len(data) // 2) + 1
        p = self._put("proj/s1.jsonl", data[:cut])
        arc = self._arc()
        sc = self._scan(arc)
        sc.build_stats()
        sc.build_stats()
        self.assertEqual(arc.status()["lines"], data[:cut].count(b"\n"))
        with open(p, "ab") as f:
            f.write(data[cut:])
        sc.build_stats()
        self.assertEqual(arc.status()["lines"], data.count(b"\n"))
        self._scan(self._arc(enable=False)).build_stats()                        # a fresh process, same db
        self.assertEqual(arc.status()["lines"], data.count(b"\n"))              # not duplicated

    def test_two_writers_do_not_duplicate_lines(self):
        data = self._session()
        p = self._put("proj/s1.jsonl", data)
        a, b = self._arc(), afi.Archive(self.apath)
        self.addCleanup(b.close)
        key = (os.stat(p).st_mtime, os.stat(p).st_size)
        a.ingest(self.root, p, "proj/s1.jsonl", "transcript", key)
        b.ingest(self.root, p, "proj/s1.jsonl", "transcript", (0, 0))
        self.assertEqual(a.status()["lines"], data.count(b"\n"))

    def test_raw_bytes_survive_exactly_including_invalid_utf8(self):
        raw = b'{"type":"user","message":{"content":"caf\xc3\xa9 \xff"}}\n{"type":"user"}\n'
        p = self._put("proj/s1.jsonl", raw)
        arc = self._arc()
        self._scan(arc).build_stats()
        src = arc.restorable(self.root, set())[0]
        self.assertEqual(arc.load_lines(src["id"]), raw.splitlines())

    def test_rewritten_file_starts_a_new_generation_and_keeps_the_old_lines(self):
        data = self._session(3)
        p = self._put("proj/s1.jsonl", data)
        arc = self._arc()
        sc = self._scan(arc)
        sc.build_stats()
        short = b"".join(data.splitlines(True)[:4])
        with open(p, "wb") as f:
            f.write(short)
        sc.build_stats()
        s = arc.status()
        self.assertEqual((s["sources"], s["generations"]), (1, 2))
        self.assertEqual(s["lines"], data.count(b"\n") + short.count(b"\n"))   # old lines kept, nothing lost
        os.remove(p)                                                              # restore uses the latest version
        src = arc.restorable(self.root, set())[0]
        self.assertEqual(len(arc.load_lines(src["id"])), short.count(b"\n"))

    def test_set_aside_copies_are_archived_but_never_counted(self):
        data = self._session()
        self._put("proj/s1.jsonl", data)
        self._put("proj/s1.orphaned-1730000000-abc.jsonl", data)
        arc = self._arc()
        st = self._scan(arc).build_stats()
        self.assertEqual(arc.status()["sources"], 2)
        base = afi.Scanner(self.root).build_stats()
        self.assertEqual(self._totals(st), self._totals(base))
        os.remove(os.path.join(self.root, "proj", "s1.jsonl"))
        # the live transcript is gone; the set-aside copy must not be restored as if it were a session
        st2 = self._scan(arc).build_stats()
        self.assertEqual((self._totals(st2)[2], st2.hc["restored_files"]), (1, 1))      # only the transcript

    # --- restore -----------------------------------------------------------
    def test_deleted_logs_are_restored_from_the_archive(self):
        data = self._session(3)
        p = self._put("-home-u-proj/s1.jsonl", data)
        self._put("-home-u-proj/s1/subagents/a1.jsonl", b"".join(
            afi.json.dumps(dict(e, isSidechain=True, uuid=str(e.get("uuid")) + "x")).encode() + b"\n"
            for e in self.entries[:2]))
        arc = self._arc()
        before = self._totals(self._scan(arc).build_stats())
        for rel in ("-home-u-proj/s1.jsonl", "-home-u-proj/s1/subagents/a1.jsonl"):
            os.remove(os.path.join(self.root, *rel.split("/")))
        self.assertEqual(self._totals(afi.Scanner(self.root).build_stats())[0], 0)         # without it: history gone
        st = self._scan(self._arc(enable=False)).build_stats()                              # new process
        self.assertEqual(self._totals(st), before)
        self.assertEqual(st.hc["restored_files"], 2)
        self.assertIn("restored", afi.build_health(st)["archive"]["note"])

    def test_restore_ignores_archives_made_for_another_logs_folder(self):
        self._put("proj/s1.jsonl", self._session())
        arc = self._arc()
        self._scan(arc).build_stats()
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        st = self._scan(arc, other).build_stats()
        self.assertEqual((len(st.sessions), st.hc["restored_files"]), (0, 0))

    def test_a_file_that_reappears_is_read_from_disk_not_double_counted(self):
        data = self._session()
        p = self._put("proj/s1.jsonl", data)
        arc = self._arc()
        sc = self._scan(arc)
        before = self._totals(sc.build_stats())
        os.remove(p)
        self.assertEqual(self._totals(sc.build_stats()), before)                 # served from the archive
        self._put("proj/s1.jsonl", data)
        st = sc.build_stats()
        self.assertEqual(self._totals(st), before)
        self.assertEqual(st.hc["restored_files"], 0)

    # --- safety --------------------------------------------------------------
    def test_size_cap_pauses_capture_and_says_so(self):
        self._put("proj/s1.jsonl", self._session())
        arc = self._arc(max_bytes=1)
        self._scan(arc).build_stats()
        s = arc.status()
        self.assertTrue(s["capped"])
        self.assertEqual(s["lines"], 0)

    def test_raw_lines_are_not_kept_in_memory_but_can_be_re_read(self):
        data = self._session()
        self._put("proj/s1.jsonl", data + b'{"type":"user","sess')              # a half-written last line
        sc = afi.Scanner(self.root)
        sc.build_stats()
        self.assertFalse(hasattr(sc, "_entries"))
        got = list(sc.raw_entries())
        self.assertEqual(got, [afi.json.loads(ln) for ln in data.splitlines()])

    def test_raw_lines_of_a_restored_file_come_from_the_archive(self):
        data = self._session()
        p = self._put("proj/s1.jsonl", data)
        arc = self._arc()
        self._scan(arc).build_stats()
        os.remove(p)
        sc = self._scan(arc)
        sc.build_stats()
        self.assertEqual(list(sc.raw_entries()), [afi.json.loads(ln) for ln in data.splitlines()])

    def test_the_archive_has_a_stable_machine_id(self):
        arc = self._arc()
        mid = arc.machine_id()
        self.assertRegex(mid, r"^[0-9a-f]{32}$")
        self.assertEqual(arc.status()["machine_id"], mid)
        other = afi.Archive(self.apath)
        self.addCleanup(other.close)
        self.assertEqual(other.machine_id(), mid)                        # same file, same machine id
        self.assertIsNone(afi.Archive(os.path.join(self.adir, "none.sqlite")).machine_id())   # no archive, no id

    def test_a_broken_archive_never_breaks_the_dashboard(self):
        data = self._session()
        self._put("proj/s1.jsonl", data)
        os.makedirs(os.path.dirname(self.apath))
        with open(self.apath, "wb") as f:
            f.write(b"this is not a sqlite database" * 50)
        arc = afi.Archive(self.apath)
        self.addCleanup(arc.close)
        st = self._scan(arc).build_stats()
        self.assertEqual(self._totals(st)[2], 1)
        self.assertTrue(arc.status()["last_error"])

    def test_unavailable_sqlite_is_handled(self):
        from unittest import mock
        with mock.patch.object(afi, "sqlite3", None):
            arc = afi.Archive(self.apath)
            self.assertFalse(arc.enable())
            self.assertFalse(arc.status()["available"])

    def test_health_reports_the_archive(self):
        self._put("proj/s1.jsonl", self._session())
        off = afi.build_health(self._scan(self._arc(enable=False)).build_stats())["archive"]
        self.assertFalse(off["enabled"])
        self.assertIn("--archive-enable", off["note"])
        on = afi.build_health(self._scan(self._arc()).build_stats())["archive"]
        self.assertTrue(on["enabled"])
        afi.json.dumps(on)


class ArchiveCommandTests(_ArchiveBase):
    """The --archive-* commands: consent wording, confirmation, and what each one changes."""

    def _run(self, answer="y", **flags):
        import types
        args = types.SimpleNamespace(archive_enable=False, archive_disable=False, archive_status=False,
                                     archive_sync=False, archive_purge=False, yes=False)
        for k, v in flags.items():
            setattr(args, k, v)
        out = []
        arc = afi.Archive(self.apath)
        self.addCleanup(arc.close)
        sc = afi.Scanner(self.root, archive=arc)
        code = afi.run_archive_command(args, sc, arc, input_fn=lambda _q: answer, print_fn=lambda *a: out.append(" ".join(map(str, a))))
        return code, "\n".join(out), arc

    def test_enable_explains_the_risk_and_does_nothing_if_declined(self):
        self._put("proj/s1.jsonl", self._session())
        code, text, arc = self._run(answer="n", archive_enable=True)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.apath))
        for must in ("secrets", "readable only by your user", "30 days", "--archive-purge", "organisation"):
            self.assertIn(must, text)

    def test_enable_confirmed_backfills_and_mentions_scheduling(self):
        data = self._session()
        self._put("proj/s1.jsonl", data)
        code, text, arc = self._run(archive_enable=True)
        self.assertEqual(code, 0)
        self.assertTrue(arc.enabled)
        self.assertIn("Captured %s lines" % format(data.count(b"\n"), ","), text)
        self.assertIn("--archive-sync", text)

    def test_enable_with_yes_flag_skips_the_question(self):
        self._put("proj/s1.jsonl", self._session())
        code, _t, arc = self._run(answer="n", archive_enable=True, yes=True)
        self.assertEqual((code, arc.enabled), (0, True))

    def test_sync_needs_the_archive_to_be_on_then_captures_new_lines(self):
        p = self._put("proj/s1.jsonl", self._session())
        code, text, _a = self._run(archive_sync=True)
        self.assertEqual(code, 1)
        self.assertIn("--archive-enable", text)
        self._run(archive_enable=True, yes=True)
        with open(p, "ab") as f:
            f.write(b'{"type":"user"}\n{"type":"user"}\n')
        code, text, _a = self._run(archive_sync=True)
        self.assertEqual(code, 0)
        self.assertIn("Captured 2 new lines", text)

    def test_disable_pauses_and_status_reports(self):
        self._put("proj/s1.jsonl", self._session())
        self._run(archive_enable=True, yes=True)
        code, text, arc = self._run(archive_disable=True)
        self.assertEqual((code, arc.enabled), (0, False))
        _c, text, _a = self._run(archive_status=True)
        self.assertIn("off (paused)", text)

    def test_purge_asks_first(self):
        self._put("proj/s1.jsonl", self._session())
        self._run(archive_enable=True, yes=True)
        code, _t, _a = self._run(answer="n", archive_purge=True)
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.apath))
        code, text, _a = self._run(answer="y", archive_purge=True)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.apath))
        self.assertIn("Archive deleted", text)

    def test_status_when_nothing_exists(self):
        code, text, _a = self._run(archive_status=True)
        self.assertEqual(code, 0)
        self.assertIn("off and nothing has been captured", text)


# ---------------------------------------------------------------------------
# Characterization ("golden") tests. Phase 3 of docs/LOG_RESILIENCE_ROADMAP.md put a canonical event layer between
# the raw log and Stats; these pin the exact Stats a deterministic "kitchen sink" log produces, so any change in
# what the parser derives shows up as a diff. To accept an intended change:
#     AFI_REGEN_GOLDEN=1 python -m unittest test_ai_fluency_insights.GoldenStatsTests
# ---------------------------------------------------------------------------

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests_data", "kitchen_sink_stats.json")


def kitchen_sink_logs():
    """{relative path: [raw entries]}: every entry kind the parser handles, fixed timestamps, two projects."""
    out = {}
    n = [0]

    def ts(day, h, m=0, s=0):
        return "2026-10-%02dT%02d:%02d:%02d.000Z" % (day, h, m, s)

    def base(sid, t, **kw):
        d = {"sessionId": sid, "timestamp": t, "cwd": "/work/app", "version": "2.1.316", "gitBranch": "main"}
        d.update(kw)
        return d

    def user(sid, t, content, **kw):
        n[0] += 1
        return base(sid, t, type="user", uuid="u%d" % n[0], message={"role": "user", "content": content}, **kw)

    def assistant(sid, t, blocks, usage="default", stop="tool_use", model="claude-sonnet-5", mid=None, **kw):
        n[0] += 1
        mid = mid or "m%d" % n[0]
        u = {"input_tokens": 12, "output_tokens": 40 + n[0], "cache_creation_input_tokens": 300,
             "cache_read_input_tokens": 5000} if usage == "default" else usage
        msg = {"id": mid, "model": model, "stop_reason": stop, "content": blocks}
        if u is not None:
            msg["usage"] = u
        return base(sid, t, type="assistant", uuid="a%d" % n[0], requestId="r" + mid, message=msg, **kw)

    def tool(name, inp, tid=None):
        n[0] += 1
        return {"type": "tool_use", "id": tid or "tu%d" % n[0], "name": name, "input": inp}

    def result(sid, t, tid, text="ok", err=False):
        b = {"type": "tool_result", "tool_use_id": tid, "content": text}
        if err:
            b["is_error"] = True
        return user(sid, t, [b])

    # ---- project A, day 12: a full working session -------------------------------------------------
    a = [{"type": "ai-title", "sessionId": "sA", "aiTitle": "Add pagination to orders"},
         {"type": "permission-mode", "permissionMode": "plan"},
         {"type": "permission-mode", "permissionMode": "default"},
         user("sA", ts(12, 9, 0), "Add pagination to the orders list.\nKeep the page size configurable."),
         assistant("sA", ts(12, 9, 0, 20), [{"type": "thinking", "thinking": "hmm"},
                                               tool("Read", {"file_path": "/work/app/orders.py"}, "t_read"),
                                               tool("Grep", {"pattern": "def list_orders"}, "t_grep")],
                   thinkingDurationMs=2400, effort="high"),
         result("sA", ts(12, 9, 0, 22), "t_read"),
         result("sA", ts(12, 9, 0, 23), "t_grep"),
         assistant("sA", ts(12, 9, 1), [tool("Edit", {"file_path": "/work/app/orders.py", "old_string": "a",
                                                        "new_string": "def page(n):\n    return n\n"}, "t_edit"),
                                          tool("Write", {"file_path": "/work/app/tests/test_orders.py",
                                                         "content": "def test_a():\n    assert 1\n\n"}, "t_write"),
                                          tool("MultiEdit", {"file_path": "/work/app/api.py", "edits": [
                                              {"old_string": "x", "new_string": "y\nz"}, {"old_string": "p", "new_string": "q"},
                                              "not-a-dict"]}, "t_multi"),
                                          tool("NotebookEdit", {"notebook_path": "/work/app/n.ipynb",
                                                                "new_source": "a\nb\nc"}, "t_nb")]),
         result("sA", ts(12, 9, 1, 5), "t_edit"), result("sA", ts(12, 9, 1, 6), "t_write"),
         result("sA", ts(12, 9, 1, 7), "t_multi"), result("sA", ts(12, 9, 1, 8), "t_nb"),
         assistant("sA", ts(12, 9, 2), [tool("Bash", {"command": "pytest -q"}, "t_test1")]),
         result("sA", ts(12, 9, 2, 30), "t_test1", "Exit code 1\nFAILED", err=True),
         assistant("sA", ts(12, 9, 3), [tool("Bash", {"command": "pytest -q"}, "t_test2")]),
         result("sA", ts(12, 9, 3, 20), "t_test2", "2 passed"),
         assistant("sA", ts(12, 9, 4), [tool("Bash", {"command": "npm run build"}, "t_build"),
                                          tool("Bash", {"command": "ruff check ."}, "t_lint")]),
         result("sA", ts(12, 9, 4, 40), "t_build"), result("sA", ts(12, 9, 4, 41), "t_lint", "E1", err=True),
         assistant("sA", ts(12, 9, 5), [tool("Bash", {"command": "git push --force origin main"}, "t_risky"),
                                          tool("Read", {"file_path": "/work/app/.env"}, "t_env"),
                                          tool("Write", {"file_path": "/work/app/secrets.json", "content": "{}"}, "t_sec")]),
         result("sA", ts(12, 9, 5, 3), "t_risky", "denied", err=True), result("sA", ts(12, 9, 5, 4), "t_env"),
         result("sA", ts(12, 9, 5, 5), "t_sec"),
         # one response logged on three lines (same message id + request id): tokens must count once
         assistant("sA", ts(12, 9, 6), [{"type": "text", "text": "Working"}], stop=None, mid="mDUP"),
         assistant("sA", ts(12, 9, 6, 1), [tool("Agent", {"subagent_type": "reviewer", "prompt": "review"}, "t_agent")],
                   stop=None, mid="mDUP"),
         assistant("sA", ts(12, 9, 6, 2), [tool("Agent", {"subagent_type": "reviewer", "prompt": "review"}, "t_agent")],
                   stop="tool_use", mid="mDUP"),
         assistant("sA", ts(12, 9, 6, 30), [tool("mcp__github__get_issue", {}, "t_mcp"), tool("TodoWrite", {}, "t_todo"),
                                              tool("WebSearch", {"query": "q"}, "t_web"), tool("Skill", {}, "t_skill"),
                                              tool("Bash", {"command": "ls"}, "t_ls")]),
         result("sA", ts(12, 9, 6, 40), "t_mcp"), result("sA", ts(12, 9, 6, 41), "t_unknown_id"),
         assistant("sA", ts(12, 9, 7), [{"type": "text", "text": "Done. Pagination added."}], stop="end_turn", model="claude-opus-5-5"),
         user("sA", ts(12, 9, 8), "no, that's wrong - the page size should default to 25"),
         assistant("sA", ts(12, 9, 8, 30), [tool("Edit", {"file_path": "/work/app/orders.py", "new_string": "PAGE = 25"}, "t_e2")],
                   usage={"input_tokens": 5, "output_tokens": 10}),
         user("sA", ts(12, 9, 8, 40), "[Request interrupted by user]"),
         user("sA", ts(12, 9, 9), "<command-name>/compact</command-name>\n<command-message>compact</command-message>"),
         user("sA", ts(12, 9, 9, 5), "<local-command-stdout>Compacted</local-command-stdout>"),
         user("sA", ts(12, 9, 10), [{"type": "text", "text": "thanks"}, {"type": "text", "text": "ship it"}]),
         assistant("sA", ts(12, 9, 10, 20), [{"type": "text", "text": "ok"}], stop="end_turn", usage={}),
         assistant("sA", ts(12, 9, 11), [{"type": "text", "text": "no usage field"}], stop="end_turn", usage=None),
         user("sA", ts(12, 9, 12), ""), user("sA", ts(12, 9, 12, 5), []),
         {"type": "system", "subtype": "stop_hook_summary", "sessionId": "sA", "timestamp": ts(12, 9, 12, 10)},
         {"type": "system", "subtype": "other", "sessionId": "sA", "timestamp": ts(12, 9, 12, 11)},
         {"type": "attachment", "sessionId": "sA", "timestamp": ts(12, 9, 12, 12), "attachment": {"type": "hook_success"}},
         {"type": "attachment", "sessionId": "sA", "timestamp": ts(12, 9, 12, 13), "attachment": {}},
         {"type": "attachment", "sessionId": "sA", "attachment": {"type": "no_timestamp"}},
         user("sA", ts(12, 9, 13), "meta line", isMeta=True),
         dict(user("sA", ts(12, 9, 14), "late night work"), timestamp=ts(12, 23, 30)),
         base("sA", ts(12, 9, 15), type="assistant", uuid="a-perm", requestId="r-perm",
              permissionDecision={"decision": "accept", "source": "user"},
              message={"id": "m-perm", "model": "claude-sonnet-5", "stop_reason": "end_turn", "content": [],
                       "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 0}}),
         {"type": "queue-operation", "operation": "enqueue", "sessionId": "sA"},
         {"type": "cost-state", "sessionId": "sA", "totalCostUSD": 1.5},
         {"type": "never-heard-of-it", "sessionId": "sA", "timestamp": ts(12, 9, 16)}]
    out["-work-app/sA.jsonl"] = a

    # ---- subagent file of the same session (sidechain), project A -----------------------------------
    out["-work-app/sA/subagents/agent-1.jsonl"] = [
        assistant("sA", ts(12, 9, 6, 10), [tool("Read", {"file_path": "/work/app/x.py"}, "tsub1"),
                                              tool("Bash", {"command": "pytest -q"}, "tsub2")], isSidechain=True),
        result("sA", ts(12, 9, 6, 12), "tsub1", err=False),
        dict(result("sA", ts(12, 9, 6, 13), "tsub2", "Exit code 2", err=True), isSidechain=True),
        dict(user("sA", ts(12, 9, 6, 14), "sidechain prompt"), isSidechain=True)]

    # ---- project B, days 12-13: a second session with another model and branch ---------------------
    b = [user("sB", ts(13, 14, 0), "explain the build"),
         assistant("sB", ts(13, 14, 1), [{"type": "text", "text": "it runs make"}], stop="end_turn",
                   model="claude-haiku-5-5", gitBranch="feature/x"),
         assistant("sB", ts(13, 14, 2), [tool("Write", {"file_path": "/work/b/mod.py", "content": "x = 1\n" * 30}, "tb1")],
                   model="some-unknown-model", gitBranch="feature/x"),
         {"type": "user", "sessionId": "sB", "message": {"content": "undated prompt"}},
         user("sB", ts(13, 14, 3), "typed after undated")]
    out["-work-b/sB.jsonl"] = b
    return out


def stats_fingerprint(st):
    """A deterministic, JSON-able picture of everything Stats derived (see GoldenStatsTests)."""
    skip = {"census_paths", "census_types", "retention", "archive",
            "adapters", "extras_keys",        # added by Phase 3: telemetry about the adapter, not derived metrics
            "reconcile", "session_model_out"}

    def norm(v):
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
        if isinstance(v, (set, frozenset)):
            return sorted((norm(x) for x in v), key=lambda x: afi.json.dumps(x, sort_keys=True))
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if isinstance(v, float):
            return round(v, 9)
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v

    return {k: norm(v) for k, v in sorted(vars(st).items()) if k not in skip}


class GoldenStatsTests(unittest.TestCase):
    """The same raw log must keep producing the same Stats, whatever happens between the raw line and Stats."""

    @classmethod
    def setUpClass(cls):
        cls._tz = os.environ.get("TZ")
        if hasattr(time, "tzset"):
            os.environ["TZ"] = "UTC"
            time.tzset()

    @classmethod
    def tearDownClass(cls):
        if hasattr(time, "tzset"):
            if cls._tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = cls._tz
            time.tzset()

    def _stats(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for rel, entries in kitchen_sink_logs().items():
            p = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(afi.json.dumps(e) + "\n")
        return afi.Scanner(root).build_stats()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the time zone")
    def test_kitchen_sink_stats_match_the_golden_snapshot(self):
        got = stats_fingerprint(self._stats())
        if os.environ.get("AFI_REGEN_GOLDEN"):
            with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
                afi.json.dump(got, f, indent=1, sort_keys=True)
                f.write("\n")
        with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
            want = afi.json.load(f)
        self.assertEqual(got.keys(), want.keys())
        for k in want:
            self.assertEqual(got[k], want[k], "Stats.%s changed" % k)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the time zone")
    def test_the_kitchen_sink_really_exercises_the_parser(self):
        st = self._stats()
        t = lambda k: sum(d[k] for d in st.days.values())      # noqa: E731
        self.assertGreater(t("loc"), 40)
        self.assertEqual(st.hc["duplicate_lines"], 2)           # one response logged on three lines
        self.assertGreaterEqual(st.hc["sidechain_entries"], 3)
        for k in ("corrections", "interruptions", "late_msgs", "tool_errors", "reads", "writes", "prompts"):
            self.assertGreater(t(k), 0, k)
        for k in ("exit_code_text", "permission_decisions", "stop_hook_summary", "thinking", "effort",
                  "tool_results_matched", "usage_complete"):
            self.assertGreater(st.hc[k], 0, k)
        self.assertEqual(sorted(st.projects), ["work/app", "work/b"])
        self.assertEqual({k for _d, k in st.safety}, {"force_push", "secret_file_touched"})
        self.assertTrue({"test", "build", "lint"} <= {k for _d, _s, k, _ok in st.cmd_runs})
        self.assertEqual(st.session_titles, {"sA": "Add pagination to orders"})
        self.assertEqual(set(st.slash), {"/compact"})
        self.assertGreater(len(st.episodes), 0)
        self.assertEqual(st.subagents, Counter({"reviewer": 1}))
        self.assertGreaterEqual(len(st.models), 4)               # sonnet, opus, haiku, unknown


class CanonicalLayerTests(unittest.TestCase):
    """Phase 3: raw line -> Record (adapter) -> Stats (projector). docs/CANONICAL_SCHEMA.md."""

    def _rec(self, entry, **kw):
        e = {"sessionId": "s", "timestamp": "2026-10-12T09:00:00.000Z", "version": "2.1.316"}
        e.update(entry)
        e.update(kw)
        return afi.adapt_entry(e)

    def _assistant(self, **msg):
        m = {"id": "m1", "model": "claude-sonnet-5", "stop_reason": "end_turn", "content": []}
        m.update(msg)
        return {"type": "assistant", "requestId": "r1", "uuid": "a1", "message": m}

    # --- adapter output --------------------------------------------------------
    def test_assistant_becomes_one_response_plus_its_tool_calls(self):
        r = self._rec(self._assistant(
            usage={"input_tokens": 3, "output_tokens": 4, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 6},
            content=[{"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/a.py", "content": "a\nb"}},
                     {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest"}},
                     {"type": "tool_use", "id": "t3", "name": "Agent", "input": {"subagent_type": "reviewer"}}]),
            thinkingDurationMs=900, effort="high")
        self.assertEqual([e["kind"] for e in r.events], ["response", "tool_call", "tool_call", "tool_call"])
        resp = r.events[0]
        self.assertEqual(resp["usage"], {"input": 3, "output": 4, "cache_write": 5, "cache_read": 6,
                                         "cache_write_1h": 0, "web_search": 0})
        self.assertEqual((resp["model"], resp["stop_reason"], resp["thinking_ms"], resp["effort"], resp["message_id"]),
                         ("claude-sonnet-5", "end_turn", 900, "high", "m1"))
        w, b, a = r.events[1:]
        self.assertEqual((w["name"], w["file_path"], w["loc"]), ("Write", "/a.py", 2))
        self.assertEqual((b["command"], b["loc"]), ("pytest", 0))
        self.assertEqual(a["subagent_type"], "reviewer")
        self.assertEqual((r.adapter, r.src, r.sid), ("claude-code-2", "assistant", "s"))

    def test_user_lines_classify_prompts_and_tool_results(self):
        typed = self._rec({"type": "user", "message": {"content": "add a test"}})
        self.assertEqual(typed.events, [{"kind": "prompt", "class": "typed", "text": "add a test"}])
        self.assertTrue(typed.human_input)
        slash = self._rec({"type": "user", "message": {"content": "<command-name>/init</command-name>"}})
        self.assertEqual(slash.events, [{"kind": "prompt", "class": "command", "command": "/init"}])
        self.assertEqual(self._rec({"type": "user", "message": {"content": "<local-command-stdout>x"}}).events[0]["class"], "local_command")
        self.assertEqual(self._rec({"type": "user", "message": {"content": "[Request interrupted by user]"}}).events[0]["class"], "interrupt")
        res = self._rec({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "boom Exit code 2", "is_error": True},
            {"type": "tool_result", "tool_use_id": "t2", "content": "fine"}]}})
        self.assertEqual([(e["tool_use_id"], e["is_error"]) for e in res.events], [("t1", True), ("t2", False)])
        self.assertIn("Exit code 2", res.events[0]["text"])
        self.assertEqual(res.events[1]["text"], "")                    # result text is only kept for failures
        self.assertFalse(res.human_input)                              # results alone are not a person typing
        mixed = self._rec({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "content": ""}, {"type": "text", "text": "and also"}]}})
        self.assertTrue(mixed.human_input)

    def test_small_entry_kinds(self):
        self.assertEqual(self._rec({"type": "ai-title", "aiTitle": "T"}).events, [{"kind": "title", "sid": "s", "title": "T"}])
        self.assertEqual(self._rec({"type": "permission-mode", "permissionMode": "plan"}).events, [{"kind": "mode", "mode": "plan"}])
        self.assertEqual(self._rec({"type": "system", "subtype": "stop_hook_summary"}).events, [{"kind": "hook_run"}])
        self.assertEqual(self._rec({"type": "attachment", "attachment": {"type": "hook_success"}}).events,
                         [{"kind": "attachment", "type": "hook_success"}])
        pd = self._rec(self._assistant(), permissionDecision={"decision": "accept", "source": "user"})
        self.assertEqual(pd.events[0], {"kind": "permission_decision", "decision": "accept", "source": "user"})
        self.assertTrue(self._rec({"type": "user", "isMeta": True, "message": {"content": "x"}}).meta)
        self.assertEqual(self._rec({"type": "queue-operation", "operation": "enqueue"}).events, [])

    def test_unfamiliar_top_level_fields_are_kept_as_extras(self):
        r = self._rec(self._assistant(), brandNewField={"deep": 1}, wireToolInputs={}, isSidechain=False)
        self.assertEqual(r.extras, {"brandNewField": {"deep": 1}})
        st = afi.Stats()
        afi.project_record(r, "p", st)
        self.assertEqual(st.extras_keys[("assistant", "brandNewField")], 1)

    def test_timestamps_are_normalised_to_utc(self):
        a = self._rec({"type": "user", "message": {"content": "x"}}, timestamp="2026-10-12T11:00:00+02:00")
        self.assertEqual(a.ts.isoformat(), "2026-10-12T09:00:00+00:00")
        self.assertEqual(a.ts_raw, "2026-10-12T11:00:00+02:00")                   # the logged text is kept verbatim
        naive = afi.adapt_entry({"type": "user", "message": {"content": "x"}, "timestamp": "2026-10-12T09:00:00"})
        self.assertEqual(naive.ts, afi.datetime(2026, 10, 12, 9, 0, 0).astimezone().astimezone(afi.timezone.utc))
        self.assertIsNone(self._rec({"type": "user", "message": {"content": "x"}}, timestamp="not a time").ts)

    def test_record_serialises_to_json(self):
        r = self._rec(self._assistant(usage={"input_tokens": 1}), brandNew=1)
        d = afi.json.loads(afi.json.dumps(r.to_dict()))
        self.assertEqual((d["canonical_version"], d["adapter"], d["events"][0]["kind"]), (1, "claude-code-2", "response"))

    # --- tolerance --------------------------------------------------------------
    def test_a_wrong_typed_token_field_no_longer_discards_the_whole_response(self):
        st = afi.Stats()
        r = self._rec(self._assistant(usage={"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": "lots"}))
        afi.project_record(r, "p", st)
        self.assertEqual(st.hc["assistant_msgs"], 1)
        self.assertEqual(sum(d["output"] for d in st.days.values()), 20)          # the readable counters still count
        self.assertEqual((st.hc["usage_complete"], dict(st.usage_missing)), (0, {"cache_read_input_tokens": 1}))
        self.assertEqual(r.events[0]["usage"]["cache_read"], 0)

    def test_malformed_shapes_do_not_raise(self):
        for e in ({"type": "attachment", "attachment": "x"}, {"type": "assistant", "message": "text"},
                  {"type": "assistant", "message": {"content": "no list", "usage": [1]}},
                  {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": "str"}]}},
                  {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "MultiEdit",
                                                                  "input": {"edits": "nope", "file_path": 5}}]}}):
            afi.project_record(self._rec(e), "p", afi.Stats())

    def test_a_line_that_is_not_an_object_is_counted_not_fatal(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "p"))
        with open(os.path.join(root, "p", "a.jsonl"), "w", encoding="utf-8") as f:
            f.write('123\n"text"\n{"type":"user","sessionId":"s","timestamp":"2026-10-12T09:00:00Z","message":{"content":"hi"}}\n')
        sc = afi.Scanner(root)
        st = sc.build_stats()
        self.assertEqual(st.parse_errors, 2)
        self.assertEqual(len(st.sessions), 1)
        self.assertEqual(len(sc._records[os.path.join(root, "p", "a.jsonl")]), 3)  # records stay 1:1 with lines

    # --- adapter registry -----------------------------------------------------------
    def test_adapter_is_chosen_by_claude_code_version(self):
        self.assertEqual(afi.adapter_for_version("2.1.316"), "claude-code-2")
        self.assertEqual(afi.adapter_for_version("2.9"), "claude-code-2")
        self.assertEqual(afi.adapter_for_version("3.0.1"), "fallback")
        self.assertEqual(afi.adapter_for_version("1.0.5"), "fallback")
        self.assertEqual(afi.adapter_for_version(None), "unversioned")
        self.assertEqual(afi.adapter_for_version("garbage"), "unversioned")

    def test_a_new_adapter_can_be_registered_for_a_new_format(self):
        def v3(entry):
            rec = afi.Record("user")
            rec.sid = "s3"
            rec.events.append({"kind": "prompt", "class": "typed", "text": "from a v3 line"})
            return rec
        with patch.object(afi, "ADAPTERS", afi.ADAPTERS + [("claude-code-3", lambda v: v[0] == 3, v3)]):
            r = afi.adapt_entry({"type": "user", "version": "3.0.0", "message": {"content": "ignored"}})
            self.assertEqual((r.adapter, r.sid, r.events[0]["text"]), ("claude-code-3", "s3", "from a v3 line"))
            self.assertEqual(afi.adapter_for_version("3.0.0"), "claude-code-3")
        self.assertEqual(afi.adapt_entry({"type": "user", "version": "3.0.0", "message": {"content": "x"}}).adapter, "fallback")

    def test_adapter_use_is_counted_per_line(self):
        st = afi.Stats()
        for v in ("2.1.1", "2.1.1", "3.0.0", None):
            e = {"type": "queue-operation"}
            if v:
                e["version"] = v
            afi.project_record(afi.adapt_entry(e), "p", st)
        self.assertEqual(dict(st.adapters), {"claude-code-2": 2, "fallback": 1, "unversioned": 1})

    # --- projector is independent of the raw format ------------------------------------
    def test_projector_works_from_a_hand_built_record(self):
        rec = afi.Record("assistant")
        rec.sid, rec.ts_raw, rec.adapter = "sX", "2026-10-12T09:00:00Z", "test"
        rec.ts = afi.datetime(2026, 10, 12, 9, 0, 0, tzinfo=afi.timezone.utc)
        rec.events = [{"kind": "response", "message_id": "m", "request_id": "r", "model": "claude-sonnet-5",
                       "stop_reason": None, "thinking_ms": None, "effort": None,
                       "usage": {"input": 100, "output": 50, "cache_write": 0, "cache_read": 0}, "usage_missing": []},
                      {"kind": "tool_call", "id": "t", "name": "Write", "command": None, "file_path": "/x/a.py",
                       "loc": 7, "subagent_type": None}]
        st = afi.Stats()
        afi.project_record(rec, "proj", st)
        self.assertEqual(sum(d["output"] for d in st.days.values()), 50)
        self.assertEqual(sum(d["loc"] for d in st.days.values()), 7)
        self.assertEqual(st.files_touched["a.py"], 7)

    # --- pricing ------------------------------------------------------------------------
    def test_dated_prices_apply_only_to_days_before_the_change(self):
        cheap = {"in": 1.0, "out": 5.0}
        base = lambda key: next(p for k, p in afi.PRICING if k == key)       # noqa: E731
        with patch.dict(afi.PRICE_HISTORY, {"sonnet-5": [("2026-06-01", cheap)]}):
            self.assertEqual(afi.price_for("claude-sonnet-5", "2026-05-31"), cheap)
            self.assertEqual(afi.price_for("claude-sonnet-5", "2026-06-01"), base("sonnet-5"))
            self.assertEqual(afi.price_for("claude-sonnet-5"), base("sonnet-5"))
            self.assertEqual(afi.price_for("claude-opus-5-5", "2020-01-01"), base("opus-5-5"))

    # --- sentinel + health ---------------------------------------------------------------
    def test_an_unverified_claude_code_version_is_reported(self):
        types = Counter({("assistant", "2.1.316"): 30, ("assistant", "3.0.2"): 25, ("atis-latch", "?"): 3})
        res = afi.analyze_schema(Counter(), types, None)
        f = [x for x in res["findings"] if x["kind"] == "unverified_version"]
        self.assertEqual([(x["versions"], x["severity"]) for x in f], [(["3.0.2"], "info")])
        self.assertIn("25 log lines", f[0]["detail"])
        known_only = afi.analyze_schema(Counter(), Counter({("assistant", "2.1.316"): 30}), None)
        self.assertFalse([x for x in known_only["findings"] if x["kind"] == "unverified_version"])

    def test_health_lists_adapters_and_unrecognised_fields(self):
        st = afi.Stats()
        for e in ({"type": "queue-operation", "version": "2.1.1", "oddKey": 1}, {"type": "queue-operation", "version": "9.0.0"}):
            afi.project_record(afi.adapt_entry(e), "p", st)
        h = afi.build_health(st)
        self.assertEqual({a["name"]: a["n"] for a in h["adapters"]}, {"claude-code-2": 1, "fallback": 1})
        self.assertEqual(h["extras"], [{"entry_type": "queue-operation", "key": "oddKey", "n": 1}])
        afi.json.dumps(h)


class CrossCheckTests(unittest.TestCase):
    """Our output tokens vs the session totals Claude Code writes into the log (cost-state)."""

    def _stats(self, ours, snapshot, model="claude-sonnet-5"):
        st = afi.Stats()
        for i, out in enumerate(ours):
            e = {"type": "assistant", "sessionId": "s1", "timestamp": "2026-10-12T09:0%d:00Z" % i, "version": "2.1.316",
                 "requestId": "r%d" % i, "uuid": "u%d" % i,
                 "message": {"id": "m%d" % i, "model": model, "stop_reason": "end_turn", "content": [],
                             "usage": {"input_tokens": 1, "output_tokens": out, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}}}
            afi.project_record(afi.adapt_entry(e), "p", st)
        if snapshot is not None:
            afi.project_record(afi.adapt_entry({"type": "cost-state", "sessionId": "s1", "totalCostUSD": 1.0,
                                                "totalLinesAdded": 5, "modelUsage": snapshot}), "p", st)
        return st

    def _cap(self, st):
        return {c["id"]: c for c in afi.build_health(st)["capabilities"]}["crosscheck"]

    @staticmethod
    def _theirs(**by_model):
        return {m: {"inputTokens": 1, "outputTokens": n, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                    "costUSD": 0.1} for m, n in by_model.items()}

    def test_cost_state_becomes_a_session_totals_event(self):
        r = afi.adapt_entry({"type": "cost-state", "sessionId": "s1", "totalCostUSD": 2.5, "totalLinesAdded": 7,
                             "modelUsage": self._theirs(**{"claude-sonnet-5": 900})})
        e = r.events[0]
        self.assertEqual((e["kind"], e["sid"], e["cost_usd"], e["lines_added"]), ("session_totals", "s1", 2.5, 7))
        self.assertEqual(e["models"]["claude-sonnet-5"]["output"], 900)
        self.assertEqual(afi.adapt_entry({"type": "cost-state", "sessionId": "s1"}).events[0]["models"], {})   # tolerant

    def test_matching_totals_pass(self):
        cap = self._cap(self._stats([3000, 2500], self._theirs(**{"claude-sonnet-5": 5500})))
        self.assertEqual(cap["status"], "ok")
        self.assertIn("100.0%", cap["detail"])

    def test_a_stale_snapshot_never_raises_a_false_alarm(self):
        # the log kept growing after the snapshot, so we legitimately count more than Claude Code did then
        self.assertEqual(self._cap(self._stats([3000, 2500, 4000], self._theirs(**{"claude-sonnet-5": 5500})))["status"], "ok")

    def test_counting_clearly_fewer_tokens_than_claude_code_is_flagged(self):
        cap = self._cap(self._stats([3000, 1000], self._theirs(**{"claude-sonnet-5": 5500})))
        self.assertEqual(cap["status"], "partial")
        self.assertIn("fewer output tokens", cap["detail"])
        self.assertIn("73%", cap["detail"])

    def test_within_tolerance_passes(self):
        self.assertEqual(self._cap(self._stats([5400], self._theirs(**{"claude-sonnet-5": 5500})))["status"], "ok")

    def test_calls_that_never_reach_the_transcript_are_not_compared(self):
        theirs = self._theirs(**{"claude-sonnet-5": 5500, "claude-haiku-5": 90000})     # a model absent from our counts
        self.assertEqual(self._cap(self._stats([3000, 2500], theirs))["status"], "ok")

    def test_small_or_missing_snapshots_are_reported_as_unavailable(self):
        small = self._cap(self._stats([300], self._theirs(**{"claude-sonnet-5": 400})))
        self.assertEqual(small["status"], "missing")
        self.assertIn("large enough", small["detail"])
        none = self._cap(self._stats([3000], None))
        self.assertEqual(none["status"], "missing")
        self.assertIn("no session totals", none["detail"].lower())

    def test_the_entry_type_is_now_read(self):
        h = afi.build_health(self._stats([3000], self._theirs(**{"claude-sonnet-5": 3000})))
        self.assertEqual({t["type"]: t["status"] for t in h["entry_types"]}["cost-state"], "handled")


def _reference_flatten(entry):
    """The original, simple census walk. flatten_entry must give exactly this, only faster (Phase 4)."""
    out = set()

    def walk(v, path, depth):
        if path:
            out.add((path, afi._jtype(v)))
        if depth >= afi.SCHEMA_MAX_DEPTH or path.rsplit(".", 1)[-1] in afi.OPAQUE_KEYS:
            return
        if isinstance(v, dict):
            is_map = path.rsplit(".", 1)[-1] in afi.MAP_KEYS
            for k, child in v.items():
                k = "<key>" if is_map else ("<id>" if afi._ID_KEY.match(str(k)) else str(k))
                walk(child, path + "." + k if path else k, depth + 1)
        elif isinstance(v, list):
            for child in v:
                walk(child, path + "[]", depth + 1)

    walk(entry, "", 0)
    return out


class FastCensusTests(unittest.TestCase):
    def _random_json(self, rng, depth=0):
        keys = ["a", "b", "message", "content", "usage", "input_schema", "wireToolInputs", "modelUsage", "x.y",
                "toolu_01ABCDEF", "123e4567-e89b-12d3-a456-426614174000", "deadbeefdeadbeefdeadbeefdeadbeef", "k1", "k2"]
        r = rng.random()
        if depth > 5 or r < 0.35:
            return rng.choice([None, True, False, 0, 3, 1.5, "s", "toolu_x", ""])
        if r < 0.7:
            return {rng.choice(keys): self._random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))}
        return [self._random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]

    def test_fast_flatten_equals_the_reference_walk(self):
        import random
        rng = random.Random(5)
        for _ in range(3000):
            e = {"type": rng.choice(["user", "assistant"]), "version": "2.1.1", "payload": self._random_json(rng)}
            self.assertEqual(afi.flatten_entry(e), _reference_flatten(e))

    def test_deeply_nested_and_huge_maps_are_bounded(self):
        deep = cur = {}
        for _ in range(40):
            cur["n"] = {}
            cur = cur["n"]
        self.assertEqual(afi.flatten_entry(deep), _reference_flatten(deep))
        wide = {"m": {"key%d" % i: i for i in range(5000)}}
        paths = {p for p, _t in afi.flatten_entry(wide)}
        self.assertLess(len(paths), 1200)                       # a runaway key space cannot grow without bound
        self.assertIn("m", paths)

    def test_census_of_shapes_expands_to_the_same_counts_as_per_entry_counting(self):
        import random
        rng = random.Random(9)
        entries = [{"type": rng.choice(["user", "assistant"]), "version": rng.choice(["2.1.1", "2.2.0"]),
                    "payload": self._random_json(rng)} for _ in range(400)]
        want_paths, want_types = Counter(), Counter()
        for e in entries:
            want_types[(e["type"], e["version"])] += 1
            for pt in _reference_flatten(e):
                want_paths[(e["type"], e["version"]) + pt] += 1
        got_paths, got_types = afi.census_entries(entries)
        self.assertEqual(got_paths, want_paths)
        self.assertEqual(got_types, want_types)
        shapes, types = afi.census_shapes(entries)
        self.assertEqual(afi.expand_census(shapes), want_paths)
        self.assertLess(len(shapes), len(entries))             # identical structures are counted once


class IncrementalProjectionTests(unittest.TestCase):
    """Phase 4: build_stats advances a running Stats with only the new records. The result must be exactly what a
    fresh scan of the same folder produces, whatever happened to the files in between."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    # -- a small random log world -------------------------------------------------------------------------------
    def _line(self, rng, sid, n, shared_ids):
        day = rng.choice([12, 13])
        t = "2026-10-%02dT%02d:%02d:%02d.000Z" % (day, rng.randint(8, 22), rng.randint(0, 59), rng.randint(0, 59))
        kind = rng.choice(["user", "assistant", "assistant", "tool", "result", "mode", "cost", "meta", "side", "title"])
        base = {"sessionId": sid, "timestamp": t, "version": "2.1.316", "cwd": "/w", "gitBranch": "main"}
        mid = rng.choice(shared_ids) if rng.random() < 0.15 else "m%s%d" % (sid, n)      # some ids repeat across files
        usage = {"input_tokens": 5, "output_tokens": rng.randint(10, 900), "cache_creation_input_tokens": 100,
                 "cache_read_input_tokens": 2000}
        if kind == "user":
            return dict(base, type="user", message={"content": rng.choice(["add a feature", "no, that is wrong", "ok"])})
        if kind == "assistant":
            return dict(base, type="assistant", requestId="r" + mid, message={
                "id": mid, "model": rng.choice(["claude-sonnet-5", "claude-opus-5-5"]), "stop_reason": rng.choice(["end_turn", None]),
                "content": [{"type": "text", "text": "x"}], "usage": usage})
        if kind == "tool":
            return dict(base, type="assistant", requestId="r" + mid, message={
                "id": mid, "model": "claude-sonnet-5", "stop_reason": "tool_use", "usage": usage, "content": [
                    {"type": "tool_use", "id": "t%s%d" % (sid, n), "name": rng.choice(["Edit", "Bash", "Read"]),
                     "input": {"file_path": "/w/a.py", "new_string": "a\nb", "command": "pytest -q"}}]})
        if kind == "result":
            return dict(base, type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t%s%d" % (sid, n - 1),
                                                                 "content": "Exit code 1", "is_error": rng.random() < .5}]})
        if kind == "mode":
            return {"type": "permission-mode", "permissionMode": rng.choice(["plan", "default"]), "sessionId": sid}
        if kind == "cost":
            return {"type": "cost-state", "sessionId": sid, "totalCostUSD": 1.0,
                    "modelUsage": {"claude-sonnet-5": {"outputTokens": rng.randint(1000, 5000)}}}
        if kind == "meta":
            return dict(base, type="user", isMeta=True, message={"content": "meta"})
        if kind == "side":
            return dict(base, type="assistant", isSidechain=True, requestId="r" + mid, message={
                "id": mid, "model": "claude-haiku-5-5", "usage": usage, "content": []})
        return {"type": "ai-title", "sessionId": sid, "aiTitle": "title %d" % n}

    def _world(self, seed, steps=45):
        import random
        rng = random.Random(seed)
        files, counter, shared = {}, [0], ["mShared1", "mShared2"]
        sc = afi.Scanner(self.root)
        used = {"advance": 0, "rebuild": 0}
        ranges = [(None, None), ("2026-10-13", None), (None, "2026-10-12")]
        for step in range(steps):
            op = rng.choice(["append"] * 5 + ["new"] * 2 + ["rewrite", "delete", "torn"])
            names = sorted(files)
            if op == "new" or not names:
                name = "-proj-%d/s%d.jsonl" % (rng.randint(0, 2), counter[0]); counter[0] += 1
                files[name] = {"sid": "S%d" % counter[0], "data": b"", "n": 0}
                op = "append"
            else:
                name = rng.choice(names)
            f = files[name]
            path = os.path.join(self.root, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if op == "delete":
                os.remove(path) if os.path.exists(path) else None
                files.pop(name)
            elif op == "rewrite":
                f["n"] = 0
                lines = [afi.json.dumps(self._line(rng, f["sid"], f["n"] + i, shared)).encode() + b"\n" for i in range(rng.randint(1, 5))]
                f["n"] += len(lines)
                with open(path, "wb") as fh:
                    fh.write(b"".join(lines))
            elif op == "torn":
                line = afi.json.dumps(self._line(rng, f["sid"], f["n"], shared)).encode()
                with open(path, "ab") as fh:
                    fh.write(line[: len(line) // 2])
            else:
                lines = [afi.json.dumps(self._line(rng, f["sid"], f["n"] + i, shared)).encode() + b"\n" for i in range(rng.randint(1, 6))]
                f["n"] += len(lines)
                with open(path, "ab") as fh:
                    fh.write(b"".join(lines))
            for mn, mx in ranges:
                got = stats_fingerprint(sc.build_stats(mn, mx))
                want = stats_fingerprint(afi.Scanner(self.root).build_stats(mn, mx))
                for k in want:
                    self.assertEqual(got[k], want[k], "seed %d step %d op %s range %s: Stats.%s" % (seed, step, op, (mn, mx), k))
        return sc

    def test_random_file_operations_match_a_fresh_scan(self):
        advances = rebuilds = 0
        for seed in range(6):
            sc = self._world(seed)
            advances += sc.proj_advances
            rebuilds += sc.proj_rebuilds
            shutil.rmtree(self.root, True)
            os.makedirs(self.root)
        self.assertGreater(advances, 60)           # the fast path really was exercised...
        self.assertGreater(rebuilds, 10)           # ...and so was the fallback

    # -- behaviour --------------------------------------------------------------------------------------------------
    def _write(self, name, lines, mode="ab"):
        path = os.path.join(self.root, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(b"".join(afi.json.dumps(e).encode() + b"\n" for e in lines))

    def _u(self, sid, t, text="hello there"):
        return {"type": "user", "sessionId": sid, "timestamp": t, "message": {"content": text}}

    def test_idle_refreshes_do_no_work_and_return_the_same_object(self):
        self._write("p/a.jsonl", [self._u("s", "2026-10-12T09:00:00Z")])
        sc = afi.Scanner(self.root)
        first = sc.build_stats()
        adv, reb = sc.proj_advances, sc.proj_rebuilds
        self.assertIs(sc.build_stats(), first)
        self.assertIs(sc.build_stats(), first)
        self.assertEqual((sc.proj_advances, sc.proj_rebuilds), (adv, reb))

    def test_a_published_stats_is_never_changed_by_later_scans(self):
        self._write("p/a.jsonl", [self._u("s", "2026-10-12T09:00:00Z")])
        sc = afi.Scanner(self.root)
        first = sc.build_stats()
        before = stats_fingerprint(first)
        self._write("p/a.jsonl", [self._u("s", "2026-10-12T09:05:00Z", "another prompt")])
        second = sc.build_stats()
        self.assertIsNot(second, first)
        self.assertEqual(stats_fingerprint(first), before)                       # the old snapshot is untouched
        self.assertEqual(sum(d["prompts"] for d in second.days.values()), 2)

    def test_appending_to_the_newest_file_takes_the_fast_path(self):
        self._write("p/a.jsonl", [self._u("s1", "2026-10-12T09:00:00Z")])
        self._write("p/b.jsonl", [self._u("s2", "2026-10-12T10:00:00Z")])
        sc = afi.Scanner(self.root)
        sc.build_stats()
        reb = sc.proj_rebuilds
        self._write("p/b.jsonl", [self._u("s2", "2026-10-12T10:05:00Z", "more")])
        self._write("p/c.jsonl", [self._u("s3", "2026-10-12T11:00:00Z")])        # a newer session starts
        sc.build_stats()
        self.assertEqual(sc.proj_rebuilds, reb)                                  # no rebuild needed
        self._write("p/a.jsonl", [self._u("s1", "2026-10-12T09:30:00Z", "older file grows")])
        sc.build_stats()
        self.assertEqual(sc.proj_rebuilds, reb + 1)                              # out of order: exact fallback

    def test_every_published_view_is_read_only_for_the_payload_builders(self):
        # the running Stats is shared with what a request is reading, so no builder may modify it
        self._write("p/a.jsonl", [self._u("s", "2026-10-12T09:00:00Z"),
                                  {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-12T09:00:05Z",
                                   "message": {"id": "m", "model": "claude-sonnet-5", "stop_reason": "end_turn",
                                               "content": [{"type": "tool_use", "id": "t", "name": "Edit",
                                                            "input": {"file_path": "/a.py", "new_string": "x\ny"}}],
                                               "usage": {"input_tokens": 1, "output_tokens": 2,
                                                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}])
        st = afi.Scanner(self.root).build_stats()
        before = stats_fingerprint(st)
        adoption = afi.build_adoption(st)
        for fn in (lambda: afi.stats_to_json(st, None), lambda: afi.build_flow(st), lambda: afi.build_quality(st),
                   lambda: afi.build_value(st), lambda: afi.build_weeks(st), lambda: afi.build_advisor(st, adoption),
                   lambda: afi.build_health(st), lambda: afi.catalog_payload(afi.build_health(st)["schema"])):
            fn()
        self.assertEqual(stats_fingerprint(st), before)

    def test_scan_level_facts_follow_the_files_without_a_rebuild(self):
        self._write("p/a.jsonl", [self._u("s", "2026-10-12T09:00:00Z")])
        sc = afi.Scanner(self.root)
        self.assertEqual(sc.build_stats().hc["decode_errors_mid"], 0)
        path = os.path.join(self.root, "p", "a.jsonl")
        with open(path, "ab") as f:
            f.write(b"{broken\n")
        st = sc.build_stats()
        self.assertEqual((st.hc["decode_errors_mid"], st.parse_errors), (1, 1))
        st2 = sc.build_stats()
        self.assertEqual((st2.hc["decode_errors_mid"], st2.parse_errors), (1, 1))      # not double counted


class CostModelTests(unittest.TestCase):
    """docs/COST_MODEL.md. Ground truth: the dollar figures Claude Code wrote into a real session log, next to its own
    token counts (numbers only, no content). The published price rules must reproduce them exactly."""

    # (model, Claude Code's token counts, Claude Code's own costUSD), from three snapshots of one real session
    SNAPSHOTS = [
        ("claude-sonnet-5-5", dict(i=1083, o=56549, cr=5522121, cw1=167274), 1.78896),
        ("claude-sonnet-5-5", dict(i=1625, o=100831, cr=10328604, cw1=228105), 2.95684),
        ("claude-sonnet-5-5", dict(i=3333, o=324484, cr=51293452, cw1=500529), 10.38297),
        ("claude-haiku-5-5", dict(i=116921, o=4946, cr=0, cw1=0), 0.01417),
    ]

    def _cost(self, model, i=0, o=0, cr=0, cw5=0, cw1=0, **kw):
        return afi.price_parts(afi.price_for(model), i, o, cw5, cw1, cr, **kw)[0]

    def test_reproduces_claude_codes_own_cost_from_its_own_token_counts(self):
        for model, t, want in self.SNAPSHOTS:
            rates = {k: v for k, v in afi.price_for(model).items() if k != "tier"}
            # a snapshot sums many requests, each under Haiku 5.5's 100k-token prompt tier, so price it at the base tier
            cost = afi.price_parts(rates, t["i"], t["o"], 0, t["cw1"], t["cr"])[0]
            self.assertAlmostEqual(cost, want, places=5, msg=model)

    def test_the_old_formula_was_wrong_in_the_way_the_investigation_found(self):
        i, o, cr, cw = 1625, 100831, 10328604, 228105                 # Sonnet 5.5 snapshot 2
        old = (i * 2 + o * 10 + cw * 2 * 1.25 + cr * 2 * 0.10) / 1e6   # 1.25x writes, 0.10x reads
        self.assertGreater(old / 2.95684, 1.2)                          # about 23% too high on a cache-heavy session

    def test_the_right_row_is_chosen_for_each_model_id(self):
        rows = {"claude-sonnet-5-5": (2, 10, 0.05), "claude-sonnet-5": (2, 10, 0.10), "claude-sonnet-4-5-20250929": (3, 15, 0.10),
                "claude-opus-5-5": (4, 20, 0.05), "claude-opus-5": (5, 25, 0.10), "claude-opus-4-5-20251101": (5, 25, 0.10),
                "claude-opus-4-1-20250805": (15, 75, 0.10), "claude-3-opus-20240229": (15, 75, 0.10),
                "claude-fable-5-1": (10, 50, 0.025), "claude-fable-5": (10, 50, 0.10),
                "claude-haiku-5-5": (0.10, 0.50, 0.10), "claude-haiku-4-5-20251001": (1, 5, 0.10),
                "claude-3-5-haiku-20241022": (0.8, 4, 0.10)}
        for model, (pin, pout, read) in rows.items():
            r, matched = afi.match_price(model)
            self.assertTrue(matched, model)
            self.assertEqual((r["in"], r["out"], r.get("read", 0.10)), (pin, pout, read), model)

    def test_unknown_models_use_the_default_and_say_so(self):
        r, matched = afi.match_price("some-brand-new-model")
        self.assertFalse(matched)
        self.assertEqual(r, afi.DEFAULT_PRICE)
        st = afi.Stats()
        e = {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-12T09:00:00Z", "version": "2.1.1",
             "message": {"id": "m", "model": "some-brand-new-model", "content": [],
                         "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0}}}
        afi.project_record(afi.adapt_entry(e), "p", st)
        self.assertEqual(st.default_priced, Counter({"some-brand-new-model": 1}))

    def test_one_hour_writes_cost_twice_the_input_price_and_five_minute_writes_1_25(self):
        self.assertAlmostEqual(self._cost("claude-sonnet-5-5", cw5=1_000_000), 2.5)
        self.assertAlmostEqual(self._cost("claude-sonnet-5-5", cw1=1_000_000), 4.0)
        self.assertAlmostEqual(self._cost("claude-sonnet-5-5", cr=1_000_000), 0.10)
        self.assertAlmostEqual(self._cost("claude-sonnet-5", cr=1_000_000), 0.20)
        self.assertAlmostEqual(self._cost("claude-fable-5-1", cr=1_000_000), 0.25)

    def test_haiku_5_5_is_priced_per_request_by_prompt_length(self):
        small = self._cost("claude-haiku-5-5", i=50_000, o=1000, cr=40_000)               # prompt 90k: base prices
        self.assertAlmostEqual(small, (50_000 * 0.10 + 1000 * 0.50 + 40_000 * 0.10 * 0.10) / 1e6)
        big = self._cost("claude-haiku-5-5", i=50_000, o=1000, cr=60_000)                 # prompt 110k: the whole request
        self.assertAlmostEqual(big, (50_000 * 0.50 + 1000 * 2.50 + 60_000 * 0.50 * 0.10) / 1e6)
        self.assertEqual(self._cost("claude-sonnet-5-5", i=500_000), 500_000 * 2 / 1e6)   # no long-context surcharge here

    def test_fast_mode_and_us_only_inference_stack_and_only_where_published(self):
        base = self._cost("claude-opus-5-5", i=1_000_000, o=1_000_000)
        self.assertAlmostEqual(base, 24.0)
        self.assertAlmostEqual(self._cost("claude-opus-5-5", i=1_000_000, o=1_000_000, speed="fast"), 48.0)
        self.assertAlmostEqual(self._cost("claude-opus-5-5", i=1_000_000, o=1_000_000, geo="us"), 24.0 * 1.1)
        self.assertAlmostEqual(self._cost("claude-opus-5-5", i=1_000_000, o=1_000_000, speed="fast", geo="us"), 48.0 * 1.1)
        self.assertAlmostEqual(self._cost("claude-sonnet-5-5", i=1_000_000, speed="fast"), 2.0)      # no fast mode for Sonnet
        self.assertAlmostEqual(self._cost("claude-opus-4-5", i=1_000_000, geo="us"), 5.0)            # 4.6 and later only
        self.assertAlmostEqual(self._cost("claude-sonnet-5-5", i=1_000_000, geo="global"), 2.0)

    def test_web_searches_cost_one_cent_each(self):
        self.assertAlmostEqual(self._cost("claude-sonnet-5-5", searches=3), 0.03)

    def test_cache_savings_use_the_models_own_rates(self):
        r = afi.price_for("claude-sonnet-5-5")
        _c, saved = afi.price_parts(r, 0, 0, 0, 100_000, 1_000_000)
        self.assertAlmostEqual(saved, (1_000_000 * 2 * 0.95 - 100_000 * 2 * 1.0) / 1e6)

    def test_the_adapter_carries_the_cache_split_speed_region_and_searches(self):
        e = {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-12T09:00:00Z", "version": "2.1.316",
             "message": {"id": "m", "model": "claude-opus-5-5", "content": [], "usage": {
                 "input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 4,
                 "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
                 "server_tool_use": {"web_search_requests": 2}, "speed": "fast", "inference_geo": "us"}}}
        resp = afi.adapt_entry(e).events[0]
        self.assertEqual((resp["usage"]["cache_write_1h"], resp["usage"]["web_search"], resp["speed"], resp["geo"]),
                         (200, 2, "fast", "us"))
        e["message"]["usage"].pop("cache_creation")                       # an older log with no split: all 5-minute
        self.assertEqual(afi.adapt_entry(e).events[0]["usage"]["cache_write_1h"], 0)
        e["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": 99999}     # cannot exceed the total
        self.assertEqual(afi.adapt_entry(e).events[0]["usage"]["cache_write_1h"], 300)

    def test_top_tier_means_the_opus_fable_families_whatever_they_cost(self):
        for m in ("claude-opus-5-5", "claude-opus-4-1", "claude-fable-5-1", "claude-mythos-5-1"):
            self.assertTrue(afi._is_top_tier(m), m)
        for m in ("claude-sonnet-5-5", "claude-haiku-5-5", "unknown"):
            self.assertFalse(afi._is_top_tier(m), m)


class PriceCheckTests(unittest.TestCase):
    """The dashboard reprices Claude Code's own token counts and compares with Claude Code's own dollar figure."""

    def _snapshot(self, model, cost, i=1625, o=100831, cr=10328604, cw1=228105, **extra):
        return {"type": "cost-state", "sessionId": "s1", "totalCostUSD": cost, "modelUsage": {model: dict({
            "inputTokens": i, "outputTokens": o, "cacheReadInputTokens": cr, "cacheCreationInputTokens": cw1,
            "cacheCreationEphemeral5mInputTokens": 0, "cacheCreationEphemeral1hInputTokens": cw1, "costUSD": cost,
            "webSearchRequests": 0}, **extra)}}

    def _cap(self, *entries, responses=()):
        st = afi.Stats()
        for e in list(responses) + list(entries):
            afi.project_record(afi.adapt_entry(e), "p", st)
        return {c["id"]: c for c in afi.build_health(st)["capabilities"]}["prices"]

    def test_correct_prices_pass(self):
        cap = self._cap(self._snapshot("claude-sonnet-5-5", 2.95684))
        self.assertEqual(cap["status"], "ok")
        self.assertIn("claude-sonnet-5-5", cap["detail"])

    def test_a_changed_price_is_reported_with_both_figures(self):
        cap = self._cap(self._snapshot("claude-sonnet-5-5", 3.60))              # Claude Code charged more than we think
        self.assertEqual(cap["status"], "partial")
        self.assertIn("$2.96", cap["detail"])
        self.assertIn("$3.60", cap["detail"])

    def test_the_old_formula_would_have_been_caught(self):
        # what the dashboard used to charge for the same tokens: 1.25x writes and 0.10x reads
        old_cost = (1625 * 2 + 100831 * 10 + 228105 * 2 * 1.25 + 10328604 * 2 * 0.10) / 1e6
        self.assertNotEqual(self._cap(self._snapshot("claude-sonnet-5-5", old_cost))["status"], "ok")

    def test_within_one_percent_passes(self):
        self.assertEqual(self._cap(self._snapshot("claude-sonnet-5-5", 2.95684 * 1.009))["status"], "ok")

    def test_tiered_haiku_totals_are_not_judged(self):
        cap = self._cap(self._snapshot("claude-haiku-5-5", 0.0142, i=116921, o=4946, cr=0, cw1=0))
        self.assertEqual(cap["status"], "ok")
        self.assertIn("prompt length", cap["detail"])                 # says why it was not judged

    def test_models_priced_by_default_are_listed(self):
        resp = {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-12T09:00:00Z", "version": "2.1.1",
                "message": {"id": "m", "model": "claude-future-9", "content": [], "usage": {
                    "input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
        cap = self._cap(responses=[resp])
        self.assertEqual(cap["status"], "partial")
        self.assertIn("claude-future-9", cap["detail"])
        self.assertIn("default", cap["detail"])

    def test_no_totals_in_the_logs_is_stated_plainly(self):
        cap = self._cap()
        self.assertEqual(cap["status"], "ok")
        self.assertIn("no Claude Code cost totals", cap["detail"])


class HealthAndCatalogTests(_LogFixture):
    def test_missing_signals_are_reported_not_zeroed(self):
        self.prompt("hi")
        self.end_turn()
        h = afi.build_health(self.stats())
        caps = {c["id"]: c for c in h["capabilities"]}
        self.assertEqual(caps["perms"]["status"], "missing")
        self.assertEqual(caps["turns"]["status"], "ok")
        self.assertEqual(h["versions"][0]["version"], "2.1.296")

    def test_catalog_is_complete_and_every_badge_in_the_page_exists(self):
        ids = [m[0] for m in afi.METRIC_CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        for m in afi.METRIC_CATALOG:
            self.assertIn(m[3], ("observed", "derived", "proxy", "estimated"))
            self.assertTrue(all(m[1:3]) and m[4] and m[5])
        import re as _re
        used = set(_re.findall(r"cb\('([a-z_0-9]+)'\)", afi.HTML))
        used |= set(_re.findall(r",C\.\w+,'([a-z_0-9]+)'", afi.HTML))
        used |= set(_re.findall(r"insHero\([^,]+,[^,]+,[^,]+,'([a-z_0-9]+)'", afi.HTML))
        self.assertTrue(used, "no badge ids found in the page")
        self.assertFalse(used - set(ids), used - set(ids))

    def test_payloads_serialise_to_json(self):
        self.prompt("go")
        self.edit()
        self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.end_turn()
        st = self.stats()
        for payload in (afi.build_flow(st), afi.build_quality(st), afi.build_value(st),
                        afi.build_health(st), afi.catalog_payload()):
            afi.json.dumps(payload)


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
        adv = afi.build_advisor(st, afi.build_adoption(st) or {})
        self.assertTrue(adv["ready"])
        verify = [r for r in adv["recs"] + adv["more"] if r["id"] == "verify"]
        self.assertEqual(len(verify), 1)
        self.assertIn("0%", verify[0]["evidence"])
        self.assertEqual(verify[0]["experiment"]["metric"], "verified_pct")
        self.assertIn("verified_pct", adv["metrics"])

    def test_verified_work_earns_a_win_not_a_warning(self):
        st = self._many_sessions(verified=True)
        adv = afi.build_advisor(st, {"end": afi.build_adoption(st).get("end") or afi.date.today().isoformat()})
        self.assertFalse([r for r in adv["recs"] if r["id"] == "verify"])
        self.assertTrue(any("check the work" in w for w in adv["wins"]))


if __name__ == "__main__":
    unittest.main()
