from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lf_v6_point_in_time_completion_audit import (  # noqa: E402
    Window,
    audit,
    current_state_replay_fill,
    point_in_time_fill,
)


class PointInTimeCompletionAuditTest(unittest.TestCase):
    def test_current_queue_cannot_label_historical_fill_windows(self) -> None:
        high_queue = Window(50.0, 90.0, 10.0, 0.40, 0.20)
        low_queue = Window(50.0, 10.0, 10.0, 0.40, 0.39)
        self.assertFalse(point_in_time_fill(high_queue))
        self.assertTrue(point_in_time_fill(low_queue))

        self.assertTrue(current_state_replay_fill(high_queue, current_queue_ahead=10.0))
        self.assertTrue(current_state_replay_fill(low_queue, current_queue_ahead=10.0))
        self.assertFalse(current_state_replay_fill(high_queue, current_queue_ahead=90.0))
        self.assertFalse(current_state_replay_fill(low_queue, current_queue_ahead=90.0))

    def test_current_book_replay_can_flip_fill_conditioned_ev(self) -> None:
        result = audit()
        self.assertEqual(result["point_in_time_completion_rate"], 0.5)
        self.assertEqual(result["replay_completion_rate_current_queue_10"], 1.0)
        self.assertEqual(result["replay_completion_rate_current_queue_90"], 0.0)
        self.assertLess(result["true_point_in_time_mean_ev"], 0.0)
        self.assertGreater(result["replay_mean_ev_current_queue_10_current_bid_039"], 0.0)
        self.assertNotEqual(
            result["true_point_in_time_mean_ev"],
            result["replay_mean_ev_current_queue_90_current_bid_039"],
        )


if __name__ == "__main__":
    unittest.main()
