import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "finalize_forward_probe.py"
spec = importlib.util.spec_from_file_location("finalize_forward_probe", SCRIPT)
finalizer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = finalizer
spec.loader.exec_module(finalizer)


class FinalizeForwardProbeTest(unittest.TestCase):
    def test_unobserved_horizons_are_null_but_observed_horizons_survive(self):
        payload = {
            "method": {},
            "results": [
                {
                    "quote_end_ts": 1000,
                    "yes": {
                        "first_fill_ts": 650,
                        "markout_60_bid_per_share": 0.01,
                        "markout_300_bid_per_share": 0.02,
                    },
                    "no": {
                        "first_fill_ts": 750,
                        "markout_60_bid_per_share": -0.01,
                        "markout_300_bid_per_share": -0.02,
                    },
                },
                {
                    "quote_end_ts": 1000,
                    "yes": {
                        "first_fill_ts": None,
                        "markout_60_bid_per_share": 0.03,
                        "markout_300_bid_per_share": 0.04,
                    },
                    "no": {},
                },
            ],
        }
        summary = finalizer.finalize(payload)
        self.assertEqual(payload["results"][0]["yes"]["markout_60_bid_per_share"], 0.01)
        self.assertEqual(payload["results"][0]["yes"]["markout_300_bid_per_share"], 0.02)
        self.assertEqual(payload["results"][0]["no"]["markout_60_bid_per_share"], -0.01)
        self.assertIsNone(payload["results"][0]["no"]["markout_300_bid_per_share"])
        self.assertIsNone(payload["results"][1]["yes"]["markout_60_bid_per_share"])
        self.assertIsNone(payload["results"][1]["yes"]["markout_300_bid_per_share"])
        self.assertEqual(summary, {"cleared_60": 1, "cleared_300": 2})
        self.assertIn("exact horizon", payload["method"]["markout_censoring"])


if __name__ == "__main__":
    unittest.main()
