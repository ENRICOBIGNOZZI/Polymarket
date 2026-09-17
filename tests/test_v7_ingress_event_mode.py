#!/usr/bin/env python3
"""Event-driven ingress is explicitly selected for the unified London release."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class IngressEventModeTest(unittest.TestCase):
    def test_explicit_release_selection_and_bounded_idle_deadline(self):
        source = (ROOT / "src/v7_external_venue_runtime.cpp").read_text()
        self.assertIn("bool event_driven_ingress = false", source)
        self.assertIn('argument == "--event-driven-ingress"', source)
        self.assertIn('"ingress_idle_deadline_ms", 5', source)
        self.assertIn('"ingress_wakeup_errors"', source)
        self.assertEqual(source.count("ingress_wakeup.get());"), 6)
        self.assertIn("wait_for_ingress();", source)
        launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
        self.assertEqual(launcher.count("--event-driven-ingress"), 1)

    def test_transport_keeps_existing_single_queue_authority(self):
        source = (ROOT / "src/v7_external_ingress.cpp").read_text()
        self.assertIn("queue_.try_push(event)", source)
        self.assertIn("wakeup_->notify()", source)
        self.assertIn("gap_pending_.store(true", source)
        self.assertIn("if (normalized_tape_ != nullptr)", source)
        wakeup = (ROOT / "src/v7_ingress_wakeup.cpp").read_text()
        self.assertIn("EFD_NONBLOCK | EFD_CLOEXEC", wakeup)
        self.assertIn("EAGAIN", wakeup)
        self.assertNotIn("std::mutex", wakeup)

    def test_probe_is_bounded_market_data_only(self):
        source = (ROOT / "src/v7_feed_race_probe.cpp").read_text()
        self.assertIn("data-stream.binance.vision", source)
        self.assertIn('"selection_or_execution_authority", false', source)
        self.assertIn("SAME_HOST_IDENTICAL_MESSAGE_ARRIVAL_NOT_EXCHANGE_ONE_WAY_LATENCY", source)
        self.assertIn("records.size() >= limit_", source)
        self.assertIn("fingerprint.size() > 1024", source)
        self.assertNotIn("post_json", source)
        self.assertNotIn('argument == "--endpoint"', source)

if __name__ == "__main__":
    unittest.main()
