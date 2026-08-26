from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowYamlContractTest(unittest.TestCase):
    def test_shell_heredoc_terminators_remain_inside_yaml_block_scalars(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip() in {"REMOTE", "PY"} and not line.startswith("          "):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_number}:{line!r}")
        self.assertEqual(offenders, [])

    def test_explicit_non_scheduler_workflows_are_unscheduled_and_non_authoritative(self) -> None:
        validator = (ROOT / "scripts" / "validate_scheduler_registry.py").read_text(encoding="utf-8")
        explicit = (
            ".github/workflows/grafana-access.yml",
            ".github/workflows/private-runtime-single-writer-validation.yml",
            ".github/workflows/operator-authority-gate.yml",
        )
        forbidden = ("gh pr merge", "git push origin HEAD:main", "git push origin main", "git push origin paper-validated", "POLYMARKET_DEPLOY_REF=")
        for relative in explicit:
            self.assertIn(f'"{relative}"', validator)
            path = ROOT / relative
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\n  schedule:\n", text, relative)
            for token in forbidden:
                self.assertNotIn(token, text, relative)

    def test_deploy_workflow_keeps_remote_terminators_indented(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertNotIn("\nREMOTE\n", deploy)
        self.assertEqual(deploy.count("\n          REMOTE\n"), 3)

    def test_deploy_is_v7_only(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["V7 live PAPER smoke"]', deploy)
        self.assertIn("champion_version=7", deploy)
        self.assertIn("polymarket_v7_runtime_info", deploy)
        self.assertIn("runs/paper_v7_live", deploy)
        for token in ("paper_v4", "paper_v5", "paper_v6", "v4-live-paper-smoke", "polymarket_v6_", "version == 6", '"5" || "$version" == "6"'):
            self.assertNotIn(token, deploy)

    def test_deploy_accepts_existing_tailscale_credentials_without_printing_them(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn("id: tailscale_auth", deploy)
        self.assertIn("TS_AUTHKEY: ${{ secrets.TS_AUTHKEY }}", deploy)
        self.assertIn("TS_OAUTH_CLIENT_ID: ${{ secrets.TS_OAUTH_CLIENT_ID }}", deploy)
        self.assertIn("TS_OAUTH_SECRET: ${{ secrets.TS_OAUTH_SECRET }}", deploy)
        self.assertNotIn('echo "$TS_AUTHKEY"', deploy)
        self.assertNotIn('echo "$TS_OAUTH_SECRET"', deploy)

    def test_server_health_uses_canonical_tailscale_grafana_route(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn("GRAFANA_HOSTNAME: mamma-portfolio", health)
        self.assertIn("GRAFANA_FQDN: mamma-portfolio.tail1bae85.ts.net", health)
        self.assertIn("GRAFANA_URL: http://mamma-portfolio.tail1bae85.ts.net", health)
        self.assertIn('tailscale ping --until-direct=false --c 1 --timeout=10s "$GRAFANA_HOSTNAME"', health)
        self.assertIn('"$GRAFANA_URL/api/health"', health)
        self.assertIn('"$GRAFANA_URL/api/search"', health)

    def test_scheduled_ci_backfills_missing_exact_head_runs(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("actions/workflows/ci.yml/runs?head_sha=${head_sha}&per_page=1", ci)
        self.assertIn("(( run_count == 0 )) || continue", ci)
        self.assertIn('gh workflow run ci.yml --ref "$head_ref" -f expected_sha="$head_sha"', ci)
        self.assertNotIn("gh pr merge", ci)

    def test_v7_live_smoke_is_exact_sha_and_advances_validation_only_on_success(self) -> None:
        smoke = (ROOT / ".github" / "workflows" / "v7-live-paper-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("name: V7 live PAPER smoke", smoke)
        self.assertIn('test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"', smoke)
        self.assertIn("paper_v7_loop.sh", smoke)
        self.assertIn("polymarket_v7_runtime_status_v1", smoke)
        self.assertIn("- name: Advance paper validated ref", smoke)
        self.assertIn("github.event_name != 'pull_request' && success()", smoke)
        self.assertIn('-f sha="$validated_sha" -F force=false', smoke)
        self.assertFalse((ROOT / ".github" / "workflows" / "v4-live-smoke.yml").exists())

    def test_bridge_listens_to_v7_smoke_not_retired_smoke(self) -> None:
        bridge = (ROOT / ".github" / "workflows" / "control-plane-event-bridge.yml").read_text(encoding="utf-8")
        self.assertIn('"V7 live PAPER smoke"', bridge)
        self.assertNotIn("v4-live-paper-smoke", bridge)


if __name__ == "__main__":
    unittest.main()
