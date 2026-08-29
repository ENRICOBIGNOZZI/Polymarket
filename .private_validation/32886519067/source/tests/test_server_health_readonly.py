from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ServerHealthReadonlyContractTest(unittest.TestCase):
    def test_macos_status_does_not_require_passwordless_sudo(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn(
            "sudo -n /usr/local/sbin/polymarket-service-control status", health
        )
        self.assertNotIn(
            "sudo -n launchctl print system/com.polymarket.autoupdate", health
        )
        self.assertIn('/bin/launchctl print "system/$label"', health)
        self.assertIn("state = running", health)
        for label in (
            "com.polymarket.awake",
            "com.polymarket.paper",
            "com.polymarket.exporter",
            "com.polymarket.prometheus",
            "com.polymarket.grafana",
        ):
            self.assertIn(label, health)
        self.assertIn(
            "/bin/launchctl print system/com.polymarket.autoupdate >/dev/null", health
        )

    def test_private_health_remains_fail_closed_on_runtime_invariants(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('test "$head_sha" = "$validated_sha"', health)
        self.assertIn("polymarket_allocator_models_expected 5", health)
        self.assertIn("scripts/v5_runtime_readiness.py", health)
        self.assertIn("--model-output-max-age 120", health)
        self.assertIn("--startup-grace 600", health)
        self.assertNotIn("scripts/runtime_action_report.py", health)
        self.assertNotIn('--output-json "$run_root/action_report.json"', health)
        self.assertNotIn('--output-markdown "$run_root/action_report.md"', health)
        self.assertIn('action_report_json="$run_root/action_report.json"', health)
        self.assertIn("polymarket_runtime_action_report_v1", health)
        self.assertIn("assert -30.0 <= age <= 180.0", health)
        self.assertIn("scripts/validate_trade_recorder_health.py", health)
        self.assertIn("--max-trade-age-seconds 300", health)
        self.assertIn("--max-request-error-rate 0.08", health)
        self.assertIn("assert -30.0 <= runtime_age <= 120.0", health)
        self.assertIn("runtime.get('proxy_ready') is True", health)
        self.assertIn("runtime_proxy_cache_age_seconds=", health)
        self.assertIn("0.0 <= proxy_cache_age <= 3600.0", health)
        self.assertIn("runtime.get('supervisor_ready') is True", health)
        self.assertIn("runtime.get('unready_components') == []", health)
        self.assertIn("required_components-set(raw_components)", health)
        self.assertIn("math.isfinite(age) and age >= 0.0", health)
        self.assertIn("math.isclose(execution_staleness,worst_component_staleness", health)
        self.assertIn("0.0 <= execution_staleness <= 120.0", health)
        self.assertIn("runtime_status_age_seconds=", health)
        self.assertIn("runtime_component_staleness=", health)
        self.assertIn("runtime_unready_components=", health)
        for component in (
            "maker",
            "micro_taker",
            "multileg_broker",
            "hard_arb",
            "external",
            "trade_recorder",
            "supervisor",
            "market_proxy",
            "relations",
            "graph_research",
            "local_factor",
            "external_bridge",
        ):
            self.assertIn(f"'{component}'", health)
        self.assertIn('test -s "$run_root/graph_research_status.json"', health)
        self.assertIn("graph['graph_mode'] == 'RESEARCH_ONLY'", health)
        self.assertIn("graph['broker_routing_enabled'] is False", health)
        self.assertIn("exploration['paper_only'] is True", health)
        self.assertIn("float(exploration['max_trade_usd']) <= 5.0", health)
        self.assertIn('test "$status_ref" = "paper-validated"', health)
        self.assertIn('test "$status_head" = "$head_sha"', health)
        self.assertIn('test "$status_validated" = "$validated_sha"', health)

    def test_private_health_reports_the_failing_runtime_stage_without_relaxing_it(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('trap health_failure ERR', health)
        self.assertIn('health_failed_stage=%s', health)
        self.assertIn('health_failed_command=%s', health)
        self.assertIn('health_failed_exit_code=%s', health)
        self.assertIn('health_status=PASS', health)
        self.assertIn("Runtime invariant result", health)
        self.assertIn("health stage marker missing", health)
        for stage in (
            "revision_identity",
            "local_endpoint_readiness",
            "metrics_contract",
            "runtime_files",
            "runtime_semantics",
            "action_report",
            "recorder_data_path",
            "runtime_integrity",
            "service_supervisor",
        ):
            self.assertIn(f'health_stage="{stage}"', health)

    def test_private_health_fails_closed_on_sustained_recorder_http_failure(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("recorder_tail_lines=", health)
        self.assertIn("recorder_success_ticks=", health)
        self.assertIn("recorder_http_failures=", health)
        self.assertIn("recorder_proxy_failures=", health)
        self.assertIn("recorder_data_failures=", health)
        self.assertIn("Gamma markets HTTP 503", health)
        self.assertIn('"$recorder_tail_lines" -ge 5', health)
        self.assertIn('"$recorder_success_ticks" -eq 0', health)
        self.assertIn(
            '"$recorder_data_failures" -ge 5', health
        )
        self.assertIn(
            "private recorder data path unhealthy: recent recorder tail has no successful tick and at least five data failures",
            health,
        )

    def test_private_health_fails_closed_on_unrecovered_state_integrity_errors(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("latest_runtime_failure()", health)
        self.assertIn("fatal: Cannot replace state file:", health)
        self.assertIn("another V6 multi-leg broker already owns", health)
        self.assertIn("FileNotFoundError:", health)
        self.assertIn("private runtime state integrity unhealthy: multileg", health)
        self.assertIn("private runtime state integrity unhealthy: hard_arb", health)
        self.assertIn("private runtime state integrity unhealthy: micro_taker", health)
        self.assertIn("^multileg_tick ", health)
        self.assertIn('^\\{.*"equity"', health)

    def test_v5_staleness_alert_respects_first_output_startup_grace(self):
        exporter = (ROOT / "monitoring" / "exporter_v5.py").read_text(encoding="utf-8")
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('MODEL_OUTPUT_MAX_AGE_SECONDS = 120.0', exporter)
        self.assertIn('MODEL_STARTUP_GRACE_SECONDS = 600.0', exporter)
        self.assertIn('allocator_events.csv', exporter)
        self.assertIn('event.get("event") not in START_EVENTS', exporter)
        self.assertIn('status_age > MODEL_OUTPUT_MAX_AGE_SECONDS', exporter)
        self.assertIn('-5.0 <= start_age <= MODEL_STARTUP_GRACE_SECONDS', exporter)
        self.assertIn('alert_staleness = 0.0 if startup_grace_active else status_age', exporter)
        self.assertIn('polymarket_model_startup_grace_active', exporter)
        self.assertIn('polymarket_model_alert_staleness_seconds', exporter)
        self.assertIn('max(polymarket_model_alert_staleness_seconds) > 60', alerts)
        self.assertNotIn('max(polymarket_model_status_age_seconds) > 60', alerts)


if __name__ == "__main__":
    unittest.main()
