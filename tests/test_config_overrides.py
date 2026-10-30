"""Per-user settings.json overrides applied on top of the built-in defaults."""

import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from ai_fluency_insights import config


class ApplyOverridesTests(unittest.TestCase):
    def setUp(self):
        # Each test mutates module-level dicts/lists, so snapshot and restore.
        self._pricing = copy.deepcopy(config.PRICING)
        self._default = copy.deepcopy(config.DEFAULT_PRICE)
        self._goals = copy.deepcopy(config.GOALS)
        self._notify = copy.deepcopy(config.NOTIFY)
        self._adoption = copy.deepcopy(config.ADOPTION)

    def tearDown(self):
        config.PRICING[:] = self._pricing
        config.DEFAULT_PRICE.clear(); config.DEFAULT_PRICE.update(self._default)
        config.GOALS.clear(); config.GOALS.update(self._goals)
        config.NOTIFY.clear(); config.NOTIFY.update(self._notify)
        config.ADOPTION.clear(); config.ADOPTION.update(self._adoption)

    def test_overrides_price_for_an_existing_model(self):
        config.apply_overrides({"pricing": {"opus": {"in": 20, "out": 100}}})
        self.assertEqual(dict(config.PRICING)["opus"], {"in": 20, "out": 100})

    def test_adds_a_new_model_price(self):
        config.apply_overrides({"pricing": {"newmodel": {"in": 1, "out": 2}}})
        self.assertEqual(dict(config.PRICING)["newmodel"], {"in": 1, "out": 2})

    def test_merges_goals_and_notify_and_adoption(self):
        config.apply_overrides({"goals": {"daily_loc": 99},
                                "notify": {"enabled": False},
                                "adoption": {"window_days": 14}})
        self.assertEqual(config.GOALS["daily_loc"], 99)
        self.assertFalse(config.NOTIFY["enabled"])
        self.assertEqual(config.ADOPTION["window_days"], 14)
        # Untouched keys keep their defaults.
        self.assertIn("daily_tokens", config.GOALS)
        self.assertEqual(config.NOTIFY["poll_seconds"], self._notify["poll_seconds"])

    def test_malformed_sections_are_ignored(self):
        config.apply_overrides({"goals": "nonsense", "pricing": 5, "bogus_key": 1})
        self.assertEqual(config.GOALS, self._goals)
        self.assertEqual(config.PRICING, self._pricing)

    def test_non_dict_top_level_is_ignored(self):
        config.apply_overrides([1, 2, 3])  # must not raise
        self.assertEqual(config.GOALS, self._goals)

    def test_load_user_settings_reads_the_data_dir_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
                json.dump({"goals": {"daily_loc": 123}}, f)
            with patch.dict(os.environ, {"AFI_DATA_DIR": d}):
                path = config.load_user_settings()
            self.assertIsNotNone(path)
            self.assertEqual(config.GOALS["daily_loc"], 123)

    def test_load_user_settings_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"AFI_DATA_DIR": d}):
                self.assertIsNone(config.load_user_settings())
            self.assertEqual(config.GOALS, self._goals)

    def test_load_user_settings_broken_json_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
                f.write("{ not valid json")
            with patch.dict(os.environ, {"AFI_DATA_DIR": d}):
                self.assertIsNone(config.load_user_settings())
            self.assertEqual(config.GOALS, self._goals)


if __name__ == "__main__":
    unittest.main()
