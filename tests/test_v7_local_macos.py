from __future__ import annotations

import subprocess
import io
import plistlib
import sys
import types
import time
from unittest import mock
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/run_v7_local_macos.sh"


class V7LocalMacosContractTest(unittest.TestCase):
    def test_local_runtime_is_exact_sha_paper_only_and_isolated(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('[[ "$(uname -s)" == "Darwin" ]]', text)
        self.assertIn('git -C "$ROOT" fetch --no-tags origin main', text)
        self.assertIn('[[ "$EXPECTED_SHA" == "$MAIN_SHA" ]]', text)
        self.assertIn("$HOME/.local/share/polymarket-v7-live", text)
        self.assertIn('[[ "$LIVE_DIR" != "$ROOT" ]]', text)
        self.assertIn("ops/update_server_v7.sh", text)
        self.assertIn("EXPECTED_DEPLOY_SHA", text)
        self.assertIn("POLYMARKET_DEPLOY_REF=main", text)
        self.assertIn("paper_only", text)
        self.assertIn("authenticated_execution", text)
        self.assertIn("real_order_submission", text)
        self.assertIn("micro_maker/status.json", text)
        self.assertIn("SHADOW_ZERO_AUTHORITY", text)
        self.assertIn("capital_authority", text)
        self.assertIn("ledger_writer_authority", text)
        self.assertIn("maker_state_path.is_file()", text)
        self.assertNotIn("ssh ", text)
        self.assertNotIn("scp ", text)

    def test_launchd_resolves_validated_homebrew_python_first(self) -> None:
        source = (ROOT / "ops/launchd/com.polymarket.v7.paper.plist.in").read_bytes()
        value = plistlib.loads(source)
        path = value["EnvironmentVariables"]["PATH"].split(":")
        self.assertLess(path.index("/opt/homebrew/bin"), path.index("/usr/bin"))
        self.assertLess(path.index("/usr/local/bin"), path.index("/usr/bin"))
        self.assertEqual(value["EnvironmentVariables"]["PM_V7_REAL_ORDER_SUBMISSION"], "0")

    def test_macos_process_local_clock_interpreter_is_rejected(self) -> None:
        text = (ROOT / "ops/v7_service_entrypoint.sh").read_text()
        guard = text.split("# MACOS_SHARED_MONOTONIC_PREFLIGHT\n", 1)[1]
        code = guard.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        for platform, version, blocked in (("darwin", (3, 9), True),
                ("darwin", (3, 10), False), ("darwin", (3, 14), False),
                ("linux", (3, 9), False)):
            fake = types.SimpleNamespace(platform=platform, version_info=version, stderr=io.StringIO())
            with mock.patch.dict(sys.modules, {"sys": fake}):
                if blocked:
                    with self.assertRaises(SystemExit) as error:
                        exec(compile(code, "service-clock-preflight", "exec"), {})
                    self.assertEqual(error.exception.code, 78)
                    self.assertIn("cross-process monotonic", fake.stderr.getvalue())
                else:
                    exec(compile(code, "service-clock-preflight", "exec"), {})

    def test_validated_interpreter_shares_monotonic_clock_with_child(self) -> None:
        if sys.platform == "darwin" and sys.version_info < (3, 10):
            self.skipTest("Unsupported interpreter is covered by fail-closed preflight")
        before = time.monotonic_ns()
        child = int(subprocess.check_output([sys.executable, "-c",
            "import time; print(time.monotonic_ns())"], text=True, timeout=10))
        after = time.monotonic_ns()
        self.assertLessEqual(before, child)
        self.assertLessEqual(child, after)

    def test_local_runtime_shell_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


if __name__ == "__main__":
    unittest.main()
