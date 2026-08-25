import http.client
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "forward_maker_transport.py"
spec = importlib.util.spec_from_file_location("forward_maker_transport", SCRIPT)
transport = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = transport
spec.loader.exec_module(transport)


class ForwardMakerTransportTest(unittest.TestCase):
    def test_incomplete_chunk_is_discarded_and_full_request_is_retried(self):
        calls = []
        sleeps = []

        def original(url, *, method="GET", body=None, timeout=20.0, retries=3):
            calls.append((url, method, body, timeout, retries))
            if len(calls) == 1:
                raise http.client.IncompleteRead(b'{"partial":')
            return {"ok": True}

        module = types.SimpleNamespace(request_json=original)
        resilient = transport.install_resilient_request_json(module, sleep=sleeps.append)
        result = resilient("https://example.invalid/data", timeout=7.0, retries=3)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], 1)
        self.assertEqual(calls[1][-1], 1)
        self.assertEqual(sleeps, [0.5])

    def test_transport_failure_remains_fail_closed_after_bounded_attempts(self):
        calls = []
        sleeps = []

        def original(url, *, method="GET", body=None, timeout=20.0, retries=3):
            calls.append(url)
            raise http.client.RemoteDisconnected("peer closed connection")

        module = types.SimpleNamespace(request_json=original)
        resilient = transport.install_resilient_request_json(module, sleep=sleeps.append)

        with self.assertRaisesRegex(RuntimeError, "request failed after 3 attempts"):
            resilient("https://example.invalid/data", retries=3)

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_non_transport_programming_error_is_not_retried(self):
        calls = []

        def original(url, *, method="GET", body=None, timeout=20.0, retries=3):
            calls.append(url)
            raise ValueError("bad caller state")

        module = types.SimpleNamespace(request_json=original)
        resilient = transport.install_resilient_request_json(module, sleep=lambda _: None)

        with self.assertRaisesRegex(ValueError, "bad caller state"):
            resilient("https://example.invalid/data", retries=3)

        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
