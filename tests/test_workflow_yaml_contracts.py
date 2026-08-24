from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowYamlContractTest(unittest.TestCase):
    def test_shell_heredoc_terminators_remain_inside_yaml_block_scalars(self) -> None:
        workflow_dir = ROOT / ".github" / "workflows"
        offenders: list[str] = []
        for path in sorted(workflow_dir.glob("*.yml")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip() in {"REMOTE", "PY"} and not line.startswith("          "):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_number}:{line!r}")
        self.assertEqual(offenders, [], "workflow heredoc terminator escaped its YAML block scalar")

    def test_deploy_workflow_keeps_all_remote_terminators_indented(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\nREMOTE\n", deploy)
        self.assertEqual(deploy.count("\n          REMOTE\n"), 3)


if __name__ == "__main__":
    unittest.main()
