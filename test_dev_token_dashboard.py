"""Tests for the token-usage notification feature in dev_token_dashboard.py.

Run with:  python -m unittest test_dev_token_dashboard -v
"""
import os
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


if __name__ == "__main__":
    unittest.main()
