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

    def test_explicit_non_scheduler_workflows_are_unscheduled_and_non_authoritative(self) -> None:
        validator = (ROOT / "scripts" / "validate_scheduler_registry.py").read_text(encoding="utf-8")
        explicit = (
            ".github/workflows/grafana-access.yml",
            ".github/workflows/private-runtime-single-writer-validation.yml",
        )
        forbidden = (
            "gh pr merge",
            "git push origin HEAD:main",
            "git push origin main",
            "git push origin paper-validated",
            "POLYMARKET_DEPLOY_REF=",
        )
        for relative in explicit:
            self.assertIn(f'"{relative}"', validator)
            path = ROOT / relative
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\n  schedule:\n", text, relative)
            for token in forbidden:
                self.assertNotIn(token, text, f"{relative} contains forbidden authority: {token}")

        private_path = ROOT / ".github" / "workflows" / "private-runtime-single-writer-validation.yml"
        if private_path.exists():
            private = private_path.read_text(encoding="utf-8")
            self.assertIn("\n  workflow_dispatch:\n", private)
            self.assertIn("\n  pull_request:\n", private)
            self.assertNotIn("\n  push:\n", private)
            self.assertNotIn("\n  workflow_run:\n", private)
            self.assertNotIn("\n  repository_dispatch:\n", private)
            self.assertIn("permissions:\n  contents: read\n", private)
            for permission in (
                "actions: write",
                "contents: write",
                "issues: write",
                "pull-requests: write",
            ):
                self.assertNotIn(permission, private)

    def test_deploy_workflow_keeps_all_remote_terminators_indented(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\nREMOTE\n", deploy)
        self.assertEqual(deploy.count("\n          REMOTE\n"), 3)

    def test_deploy_accepts_existing_authkey_or_oauth_credentials_without_printing_them(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("id: tailscale_auth", deploy)
        self.assertIn("TS_AUTHKEY: ${{ secrets.TS_AUTHKEY }}", deploy)
        self.assertIn("TS_OAUTH_CLIENT_ID: ${{ secrets.TS_OAUTH_CLIENT_ID }}", deploy)
        self.assertIn("TS_OAUTH_SECRET: ${{ secrets.TS_OAUTH_SECRET }}", deploy)
        self.assertIn("if: steps.tailscale_auth.outputs.mode == 'authkey'", deploy)
        self.assertIn("if: steps.tailscale_auth.outputs.mode == 'oauth'", deploy)
        self.assertIn("oauth-client-id: ${{ secrets.TS_OAUTH_CLIENT_ID }}", deploy)
        self.assertIn("oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}", deploy)
        self.assertIn("tags: ${{ vars.TS_TAGS || 'tag:ci' }}", deploy)
        self.assertNotIn('echo "$TS_AUTHKEY"', deploy)
        self.assertNotIn('echo "$TS_OAUTH_SECRET"', deploy)

    def test_server_health_uses_canonical_tailscale_serve_grafana_route(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("GRAFANA_HOSTNAME: mamma-portfolio", health)
        self.assertIn("GRAFANA_FQDN: mamma-portfolio.tail1bae85.ts.net", health)
        self.assertIn("GRAFANA_URL: http://mamma-portfolio.tail1bae85.ts.net", health)
        self.assertIn('tailscale ping --until-direct=false --c 1 --timeout=10s "$GRAFANA_HOSTNAME"', health)
        self.assertIn('getent hosts "$GRAFANA_FQDN"', health)
        self.assertIn('"$GRAFANA_URL/api/health"', health)
        self.assertIn('"$GRAFANA_URL/api/search"', health)
        self.assertNotIn('"http://$SERVER_HOST:3000/api/health"', health)
        self.assertNotIn('"http://$SERVER_HOST:3000/api/search"', health)

    def test_scheduled_ci_backfills_api_updated_pr_heads_exactly_once(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("  actions: write\n", ci)
        self.assertIn("  pull-requests: read\n", ci)
        self.assertIn(
            "if: github.event_name == 'schedule' && matrix.build_type == 'Release'",
            ci,
        )
        self.assertIn("actions/workflows/ci.yml/runs?head_sha=${head_sha}&per_page=1", ci)
        self.assertIn("(( run_count == 0 )) || continue", ci)
        self.assertIn("repos/${repo}/pulls/${pr_number}", ci)
        self.assertIn("[[ \"$current_sha\" == \"$head_sha\" ]] || continue", ci)
        self.assertIn(
            'gh workflow run ci.yml --ref "$head_ref" -f expected_sha="$head_sha"',
            ci,
        )
        self.assertNotIn("gh pr merge", ci)
        self.assertNotIn("paper-validated", ci)
        self.assertNotIn("POLYMARKET_DEPLOY_REF", ci)

    def test_live_smoke_retries_telemetry_publish_before_failing_validation(self) -> None:
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("for attempt in 1 2 3 4 5; do", smoke)
        self.assertIn('current_sha="$(gh api "${endpoint}?ref=telemetry" --jq .sha', smoke)
        self.assertIn('if gh api "${args[@]}" >/dev/null 2>"$publish_error"; then', smoke)
        self.assertIn('if [[ "$attempt" -eq 5 ]]; then', smoke)
        self.assertIn("sleep $((attempt * 2))", smoke)
        self.assertIn("- name: Advance paper validated ref", smoke)
        self.assertNotIn("continue-on-error: true", smoke)


if __name__ == "__main__":
    unittest.main()
