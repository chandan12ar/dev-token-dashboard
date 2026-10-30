"""Notification threshold maths: milestones, steps, weighted tokens, per-session tracking."""

import unittest

from ai_fluency_insights.config import NOTIFY
from ai_fluency_insights.notifications.thresholds import (
    SessionTokenTracker,
    milestones_crossed,
    steps_crossed,
    weighted_tokens,
)


class MilestonesCrossedTests(unittest.TestCase):
    def test_single_milestone_crossed(self):
        # 50000/200000 = 25%
        self.assertEqual(milestones_crossed(0, 50_000, 200_000), [25])

    def test_no_milestone_crossed_when_still_below(self):
        self.assertEqual(milestones_crossed(0, 10_000, 200_000), [])

    def test_multiple_milestones_crossed_in_one_jump(self):
        self.assertEqual(milestones_crossed(0, 200_000, 200_000),
                          [25, 50, 75, 85, 100])

    def test_already_past_milestone_does_not_refire(self):
        # prev is already above 50% threshold -> no new crossing
        self.assertEqual(milestones_crossed(120_000, 130_000, 200_000), [])

    def test_zero_total_disables_milestones(self):
        self.assertEqual(milestones_crossed(0, 999_999, 0), [])

    def test_drop_then_regrowth_refires_same_milestone(self):
        # simulates a context compaction: usage drops, then climbs back past 50%
        self.assertEqual(milestones_crossed(90_000, 110_000, 200_000), [50])


class StepsCrossedTests(unittest.TestCase):
    def test_single_step_crossed(self):
        self.assertEqual(steps_crossed(8_000, 12_000, 10_000), [10_000])

    def test_multiple_steps_crossed_in_one_jump(self):
        self.assertEqual(steps_crossed(0, 25_000, 10_000),
                          [10_000, 20_000])

    def test_no_step_crossed_when_still_below(self):
        self.assertEqual(steps_crossed(1_000, 4_999, 5_000), [])

    def test_no_step_crossed_on_no_increase(self):
        self.assertEqual(steps_crossed(10_000, 10_000, 5_000), [])

    def test_zero_step_disables(self):
        self.assertEqual(steps_crossed(0, 999_999, 0), [])


class WeightedTokensTests(unittest.TestCase):
    """Plan-window accounting must track quota-weighted usage, not a raw
    token sum — cache reads are ~0.1x the cost of fresh input (see PRICING),
    so a chat-heavy session with heavy tool-call caching would otherwise look
    like it burned far more quota than it actually did."""

    def test_input_and_output_weighted_by_pricing_ratio(self):
        # every PRICING tier prices output at 5x input, so output carries a
        # flat 5x weight regardless of model
        self.assertEqual(weighted_tokens(1_000, 100, 0, 0), 1_000 + 500)

    def test_cache_write_weighted_at_1_25x(self):
        self.assertEqual(weighted_tokens(0, 0, 1_000, 0), 1_250)

    def test_cache_read_weighted_at_0_1x(self):
        self.assertEqual(weighted_tokens(0, 0, 0, 1_000), 100)


class SessionTokenTrackerTests(unittest.TestCase):
    def make_tracker(self, **notify_overrides):
        notify = dict(NOTIFY)
        notify.update(notify_overrides)
        return SessionTokenTracker(notify=notify)

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
        t = SessionTokenTracker(notify=dict(NOTIFY, context_window=0,
                                                 session_step=10_000,
                                                 task_step=0),
                                     session_total=9_500)
        events = t.on_usage(400, 0, 0, 0)  # crosses to 9900, still under 10k
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
