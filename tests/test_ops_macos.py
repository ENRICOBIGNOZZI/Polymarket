from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSOpsContractTest(unittest.TestCase):
    def test_autoupdate_launchd_exports_updater_environment(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")

        self.assertIn('${POLYMARKET_APP_DIR:-$HOME/polymarket}', updater)
        self.assertIn('<key>EnvironmentVariables</key>', installer)
        self.assertIn('<key>HOME</key>', installer)
        self.assertIn('<key>PATH</key>', installer)
        self.assertIn('<key>POLYMARKET_APP_DIR</key>', installer)
        self.assertIn('$BREW_PREFIX/bin:$BREW_PREFIX/sbin', installer)

    def test_homebrew_discovery_survives_minimal_path(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")

        for script in (installer, updater):
            self.assertIn('find_brew()', script)
            self.assertIn('/opt/homebrew/bin/brew', script)
            self.assertIn('/usr/local/bin/brew', script)
            self.assertIn('BREW_BIN="$(find_brew)"', script)
            self.assertIn('"$BREW_BIN" --prefix', script)

    def test_autoupdate_remains_paper_deploy_only(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        self.assertIn('update_server_macos.sh', installer)
        self.assertNotIn('tiny_live_pilot.py', installer)
        self.assertNotIn('--execute', installer)

    def test_grafana_passwordless_access_is_tailnet_viewer_only(self):
        runtime = (ROOT / "ops" / "apply_runtime_config_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        finish = (ROOT / "ops" / "finish_bootstrap_macos.sh").read_text(encoding="utf-8")

        # Grafana itself remains loopback-only; Tailscale Serve is the sole remote route.
        self.assertIn('http_addr = 127.0.0.1', runtime)
        self.assertIn('root_url = http://127.0.0.1:3000/', runtime)
        self.assertNotIn('http_addr = 0.0.0.0', runtime)
        self.assertIn('find_tailscale()', runtime)
        self.assertIn('/Applications/Tailscale.app/Contents/MacOS/Tailscale', runtime)
        self.assertIn('serve --bg --http=3000 localhost:3000', runtime)
        self.assertIn('exposure=tailscale-serve', runtime)

        self.assertIn('[auth]\ndisable_login_form = true', runtime)
        self.assertIn('disable_signout_menu = true', runtime)
        self.assertIn('[auth.basic]\nenabled = false', runtime)
        self.assertIn('[auth.anonymous]\nenabled = true', runtime)
        self.assertIn('org_role = Viewer', runtime)
        self.assertNotIn('org_role = Admin', runtime)
        self.assertIn(
            'default_home_dashboard_path = $APP_DIR/monitoring/grafana/dashboards/polymarket-latest.json',
            runtime,
        )
        self.assertIn('reporting_enabled = false', runtime)
        self.assertIn('apply_runtime_config_macos.sh', updater)
        self.assertIn('apply_runtime_config_macos.sh', finish)
        self.assertIn('http://127.0.0.1:3000/api/search', updater)
        self.assertIn('http://127.0.0.1:3000/api/search', finish)

    def test_autoupdate_deploys_only_live_smoke_validated_ref(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        linux_updater = (ROOT / "ops" / "update_server.sh").read_text(encoding="utf-8")
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")

        self.assertIn('${POLYMARKET_DEPLOY_REF:-paper-validated}', updater)
        self.assertIn('git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"', updater)
        self.assertIn('origin/$DEPLOY_REF', updater)
        self.assertIn('write_status awaiting_validation', updater)
        self.assertIn('paper_runtime_healthy()', updater)
        self.assertIn('full_runtime_healthy()', updater)
        self.assertIn('wait_for_runtime_health()', updater)
        self.assertIn('write_status repaired', updater)
        self.assertIn('polymarket-service-control restart', updater)
        self.assertIn('recorder_alive', updater)
        self.assertIn('broker_alive', updater)
        self.assertIn('deploy_ref=%s', updater)
        self.assertIn('validated=%s', updater)
        self.assertIn('origin_main=%s', updater)

        self.assertIn('${POLYMARKET_DEPLOY_REF:-paper-validated}', linux_updater)
        self.assertIn('git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"', linux_updater)
        self.assertIn('NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"', linux_updater)
        self.assertIn('git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA"', linux_updater)
        self.assertIn('validated_ref=%s', linux_updater)
        self.assertNotIn('NEW_SHA="$(git rev-parse "origin/$BRANCH")"', linux_updater)

        self.assertIn('git fetch -q origin main paper-validated', health)
        self.assertIn('origin/paper-validated', health)
        self.assertIn('test "$head_sha" = "$validated_sha"', health)
        self.assertIn('git merge-base --is-ancestor "$validated_sha" "$main_sha"', health)
        self.assertIn('test "$status_ref" = "paper-validated"', health)
        self.assertIn('test "$status_validated" = "$validated_sha"', health)
        self.assertIn('up_to_date|deployed|repaired', health)

        self.assertIn('Advance paper validated ref', smoke)
        self.assertIn('git/refs/heads/paper-validated', smoke)
        self.assertIn("github.event_name != 'pull_request' && success()", smoke)
        self.assertNotIn("github.head_ref == 'implement/paper-live-oos-pilot-v4'", smoke)
        self.assertIn('group: v4-live-paper-smoke-${{ github.ref }}', smoke)

        telemetry_pos = smoke.index('- name: Publish latest public telemetry')
        artifact_pos = smoke.index('- name: Upload live diagnostics')
        advance_pos = smoke.index('- name: Advance paper validated ref')
        self.assertLess(telemetry_pos, artifact_pos)
        self.assertLess(artifact_pos, advance_pos)
        self.assertNotIn('continue-on-error: true', smoke[telemetry_pos:artifact_pos])
        self.assertIn('if-no-files-found: error', smoke[artifact_pos:advance_pos])

        # Deployment must follow the successful validation workflow, not the
        # earlier main push where paper-validated can still point to old code.
        self.assertIn('workflow_run:', deploy)
        self.assertIn('workflows: ["v4-live-paper-smoke"]', deploy)
        self.assertIn('types: [completed]\n    branches: [main]', deploy)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", deploy)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", deploy)
        self.assertIn("github.event.workflow_run.event != 'pull_request'", deploy)
        self.assertNotIn('push:\n    branches: [main]', deploy)
        self.assertIn('EXPECTED_VALIDATED_SHA', deploy)
        self.assertIn('test "$validated_sha" = "$EXPECTED_VALIDATED_SHA"', deploy)
        self.assertRegex(deploy, r'git fetch(?: -q)? origin main paper-validated')
        self.assertIn('validated_sha="$(git rev-parse origin/paper-validated)"', deploy)
        self.assertIn('git show "$validated_sha:$updater_path"', deploy)
        self.assertIn('POLYMARKET_DEPLOY_REF=paper-validated bash "$updater"', deploy)
        self.assertIn('test "$head_sha" = "$validated_sha"', deploy)
        self.assertIn('git merge-base --is-ancestor "$validated_sha" "$main_sha"', deploy)
        self.assertIn('paper-server-deploy-${{ github.run_id }}', deploy)
        self.assertNotIn('test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"', deploy)

        # Produce private health evidence immediately after deployment as well
        # as on the existing hourly schedule, and only from the main chain.
        self.assertIn('workflow_run:', health)
        self.assertIn('workflows: ["deploy-paper-server"]', health)
        self.assertIn('types: [completed]\n    branches: [main]', health)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", health)

        # The operator route and explanatory action report are now contractual.
        self.assertIn('http://$SERVER_HOST:3000/api/health', health)
        self.assertIn('runtime_action_report.py', health)
        self.assertIn('runtime-action-report.md', health)
        self.assertIn('action_report.json', health)


if __name__ == "__main__":
    unittest.main()
