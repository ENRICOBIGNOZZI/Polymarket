from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/run_v7_local_macos_fallback.sh"


class V7LocalMacosFallbackContractTest(unittest.TestCase):
    def test_fallback_is_exact_sha_paper_only_and_isolated(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('[[ "$(uname -s)" == "Darwin" ]]', text)
        self.assertIn("origin main paper-validated", text)
        self.assertIn('[[ "$MAIN_SHA" == "$VALIDATED_SHA" ]]', text)
        self.assertIn('[[ "$EXPECTED_SHA" == "$MAIN_SHA" ]]', text)
        self.assertIn("$HOME/.local/share/polymarket-v7-live", text)
        self.assertIn('[[ "$LIVE_DIR" != "$ROOT" ]]', text)
        self.assertIn("ops/update_server_v7.sh", text)
        self.assertIn("EXPECTED_VALIDATED_SHA", text)
        self.assertIn("POLYMARKET_DEPLOY_REF=paper-validated", text)
        self.assertIn("paper_only", text)
        self.assertIn("authenticated_execution", text)
        self.assertIn("real_order_submission", text)
        self.assertNotIn("ssh ", text)
        self.assertNotIn("scp ", text)

    def test_fallback_shell_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


if __name__ == "__main__":
    unittest.main()
