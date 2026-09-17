import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / 'monitoring'
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

import exporter_v7 as exporter


class SlowDiagnosticsCacheTests(unittest.TestCase):
    SHA = 'a' * 40

    def _write(self, root: Path, **overrides) -> Path:
        value = {
            'schema': 'polymarket_v7_slow_monitoring_diagnostics_v1',
            'timestamp': 950,
            'model_sha': self.SHA,
            'paper_only': True,
            'authenticated_execution': False,
            'real_order_submission': False,
            'real_capital_at_risk': False,
            'execution_authority': False,
            'refresh_duration_seconds': 1.25,
            'maker_fillability': {'present': True},
            'maker_latency': {'present': True},
        }
        value.update(overrides)
        path = root / 'current.json'
        path.write_text(json.dumps(value))
        return path

    def test_recent_safe_matching_cache_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = exporter._slow_diagnostics_cache(self._write(Path(tmp)), self.SHA, 1000)
            self.assertTrue(result['valid'])
            self.assertEqual(result['age'], 50)
            self.assertEqual(result['refresh_duration_seconds'], 1.25)
            self.assertTrue(result['maker_fillability']['present'])
            self.assertTrue(result['maker_latency']['present'])

    def test_stale_cache_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = exporter._slow_diagnostics_cache(
                self._write(Path(tmp), timestamp=800), self.SHA, 1000
            )
            self.assertFalse(result['valid'])
            self.assertEqual(result['age'], 200)

    def test_unsafe_or_mismatched_cache_is_invalid(self) -> None:
        unsafe = [
            {'real_capital_at_risk': True},
            {'execution_authority': True},
            {'authenticated_execution': True},
            {'real_order_submission': True},
            {'model_sha': 'b' * 40},
        ]
        for overrides in unsafe:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                result = exporter._slow_diagnostics_cache(
                    self._write(Path(tmp), **overrides), self.SHA, 1000
                )
                self.assertFalse(result['valid'])


if __name__ == '__main__':
    unittest.main()
