from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).with_name("run_v7_test_file.py")


class TestV7Runner(unittest.TestCase):
    def run_source(self, source: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test_probe.py"
            path.write_text(source)
            return subprocess.run([sys.executable, str(RUNNER), str(path)],
                                  capture_output=True, text=True, check=False)

    def test_failed_function_is_not_a_false_green(self):
        result = self.run_source("def test_fails():\n    assert False\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1 failed", result.stdout)

    def test_native_pytest_fixtures_are_executed(self):
        result = self.run_source("def test_fixture(tmp_path, monkeypatch):\n"
                                "    monkeypatch.setenv('V7_RUNNER_PROBE', 'yes')\n"
                                "    p = tmp_path / 'evidence'\n"
                                "    p.write_text('observed')\n"
                                "    assert p.read_text() == 'observed'\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 passed", result.stdout)

    def test_no_test_cannot_pass(self):
        self.assertEqual(self.run_source("VALUE = 1\n").returncode, 5)

    def test_script_only_failure_is_preserved(self):
        result = self.run_source("if __name__ == '__main__':\n    raise SystemExit(7)\n")
        self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
