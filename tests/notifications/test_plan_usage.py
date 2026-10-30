"""Plan-usage window estimate and the official rate-limit capture."""

import os
import tempfile
import unittest

from ai_fluency_insights.notifications.plan_usage import RATE_LIMITS_STALE_SECONDS, PlanWindowTracker, load_rate_limits
from ai_fluency_insights.notifications.state import save_notify_state


class PlanWindowTrackerTests(unittest.TestCase):
    """Approximates the Pro/Max app's rolling ~5h 'Current session' limit:
    a single global window (not per Claude Code session) that starts on
    first usage and resets fully 5h later."""

    WINDOW_SECONDS = 5 * 3600

    def test_first_event_starts_window_and_may_cross_milestone(self):
        t = PlanWindowTracker(ceiling=100_000)
        events = t.add(ts_epoch=1_000, tokens=30_000)
        self.assertEqual(events, [25])
        self.assertEqual(t.window_start, 1_000)
        self.assertEqual(t.window_total, 30_000)

    def test_second_event_within_window_accumulates(self):
        t = PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 30_000)
        events = t.add(1_000 + 3600, 30_000)  # 60% total now
        self.assertEqual(events, [50])
        self.assertEqual(t.window_total, 60_000)

    def test_event_at_exactly_5h_starts_a_new_window(self):
        t = PlanWindowTracker(ceiling=100_000)
        t.add(0, 90_000)
        events = t.add(self.WINDOW_SECONDS, 10_000)
        self.assertEqual(t.window_start, self.WINDOW_SECONDS)
        self.assertEqual(t.window_total, 10_000)
        self.assertEqual(events, [])  # 10% of the fresh window, no crossing

    def test_event_just_under_5h_stays_in_same_window(self):
        t = PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        t.add(self.WINDOW_SECONDS - 1, 10_000)
        self.assertEqual(t.window_start, 0)
        self.assertEqual(t.window_total, 20_000)

    def test_zero_ceiling_disables_milestones(self):
        t = PlanWindowTracker(ceiling=0)
        events = t.add(0, 50_000)
        self.assertEqual(events, [])

    def test_pct_caps_at_100(self):
        t = PlanWindowTracker(ceiling=100_000)
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
        t = PlanWindowTracker(ceiling=100_000)
        t.add(0, 80_000)
        expired_now = self.WINDOW_SECONDS + 500
        self.assertEqual(t.resets_in_seconds(expired_now), 0)
        self.assertEqual(t.pct(now_epoch=expired_now), 0)

    def test_resets_in_seconds_counts_down_from_window_start(self):
        t = PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=1_000 + 3600),
                          self.WINDOW_SECONDS - 3600)

    def test_resets_in_seconds_floors_at_zero_past_the_boundary(self):
        t = PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=self.WINDOW_SECONDS + 500), 0)

    def test_state_round_trips_into_a_new_tracker(self):
        t = PlanWindowTracker(ceiling=100_000, window_start=500, window_total=20_000)
        restored = PlanWindowTracker(ceiling=100_000, **t.state())
        self.assertEqual(restored.window_start, 500)
        self.assertEqual(restored.window_total, 20_000)


class LoadRateLimitsTests(unittest.TestCase):
    """load_rate_limits() reads the file the statusLine hook writes -- see
    ~/.claude/statusline.js -- and must treat a stale or missing file as
    unavailable rather than serve a number that's stopped moving."""

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_rate_limits("/no/such/path.json"))

    def test_fresh_file_returns_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            save_notify_state(path, data)
            self.assertEqual(load_rate_limits(path, now=1000.0 + 60), data)

    def test_stale_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            save_notify_state(path, data)
            stale_now = 1000.0 + RATE_LIMITS_STALE_SECONDS + 1
            self.assertIsNone(load_rate_limits(path, now=stale_now))

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertIsNone(load_rate_limits(path))


if __name__ == "__main__":
    unittest.main()
