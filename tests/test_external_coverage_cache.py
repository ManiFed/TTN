"""external_coverage's in-memory cache must be keyed by window_days too.

external_observation_count's result is a function of window_days (the AAVSO/
ALeRCE queries scale their count by it), but the cache used to key by
target name alone. Any two callers asking about the same target with
different windows within the 6h TTL would silently get back whichever
answer was cached first, for the wrong window.
"""

import unittest
from unittest.mock import patch

from cloud import external_coverage


class CacheKeyTest(unittest.TestCase):
    def setUp(self):
        external_coverage._cache.clear()
        self.addCleanup(external_coverage._cache.clear)

    def test_different_window_days_are_not_conflated(self):
        target = {"name": "SS Cyg", "target_type": "CV"}
        with patch.object(external_coverage, "_aavso_count",
                          side_effect=lambda name, window: window * 10):
            short = external_coverage.external_observation_count(target, window_days=7)
            long = external_coverage.external_observation_count(target, window_days=30)
        self.assertEqual(short, 70)
        self.assertEqual(long, 300)

    def test_same_window_days_is_still_cached(self):
        target = {"name": "SS Cyg", "target_type": "CV"}
        calls = []
        with patch.object(external_coverage, "_aavso_count",
                          side_effect=lambda name, window: calls.append(1) or 42):
            external_coverage.external_observation_count(target, window_days=30)
            external_coverage.external_observation_count(target, window_days=30)
        self.assertEqual(len(calls), 1, "second call within TTL must hit the cache")


if __name__ == "__main__":
    unittest.main()
