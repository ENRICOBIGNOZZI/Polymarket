from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "monitoring/grafana/dashboards"


def loaded() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(DASHBOARDS.glob("*.json"))]


def panels(value: dict):
    stack = list(value.get("panels") or [])
    while stack:
        panel = stack.pop()
        if not isinstance(panel, dict):
            continue
        yield panel
        stack.extend(panel.get("panels") or [])


def test_every_operator_dashboard_is_explicitly_crypto() -> None:
    values = loaded()
    assert {dashboard["uid"] for dashboard in values} == {
        "polymarket-v7", "polymarket-v7-external-fair",
        "polymarket-v7-latency", "polymarket-v7-multi-crypto"
    }
    for dashboard in values:
        assert "Crypto" in dashboard.get("title", "")
        assert "crypto" in (dashboard.get("tags") or [])
        for link in dashboard.get("links") or []:
            assert "Crypto" in str(link.get("title") or "")


def test_visible_dashboard_surface_has_no_retired_multi_engine_or_arb_language() -> None:
    forbidden_titles = {
        "Engine reporting source · configured is not running",
        "Engine count",
        "Configured engines · not execution proof",
        "External-Fair Latency Chain",
    }
    for dashboard in loaded():
        for panel in panels(dashboard):
            assert panel.get("title") not in forbidden_titles
            for target in panel.get("targets") or []:
                if not isinstance(target, dict):
                    continue
                assert target.get("legendFormat") not in {"structural", "arb events"}
                assert "polymarket_execution_arbs" not in str(target.get("expr") or "")


def test_crypto_only_alert_scope_is_one_live_algorithm() -> None:
    alerts = (ROOT / "monitoring/v7_alerts.yml").read_text(encoding="utf-8")
    assert "polymarket-v7-crypto-hard-safety" in alerts
    assert "polymarket_v7_live_algorithm_count != 1" in alerts
    for stale in ("two-engine", "!= 2", "two canonical algorithms"):
        assert stale not in alerts


def test_control_room_uses_crypto_specific_runtime_language() -> None:
    main = json.loads((DASHBOARDS / "polymarket-v7.json").read_text(encoding="utf-8"))
    serialized = json.dumps(main, ensure_ascii=False)
    for required in (
        "Crypto PAPER Control Room",
        "Crypto engine telemetry source",
        "Crypto New-Risk Authority",
        "Crypto live algorithm count",
        "Crypto engine configured · not execution proof",
        "Crypto execution diagnostics · event counts, not unique opportunities",
        "Crypto accounting residuals · component state minus ledger",
        "Crypto universe coverage · markets",
    ):
        assert required in serialized


def test_multi_crypto_keeps_independent_live_telemetry_visible() -> None:
    dashboard = json.loads((DASHBOARDS / "polymarket-v7-multi-crypto.json").read_text(encoding="utf-8"))
    by_title = {panel.get("title"): panel for panel in panels(dashboard)}
    assert "LIVE TELEMETRY — always visible" in by_title
    scrape = by_title["Exporter"]["targets"][0]["expr"]
    usable = by_title["Exporter Snapshot Usable"]["targets"][0]["expr"]
    age = by_title["Data age"]["targets"][0]["expr"]
    assert 'up{job="polymarket-v7"' in scrape
    assert "polymarket_v7_exporter_snapshot_usable" not in scrape
    assert "or vector(0)" in scrape
    assert "polymarket_v7_exporter_snapshot_usable" in usable
    assert "or vector(0)" in usable
    assert "polymarket_v7_exporter_snapshot_age_seconds" in age



def test_provisioning_names_and_manifest_are_crypto_only() -> None:
    manifest = json.loads((ROOT / "monitoring/v7_monitoring_manifest.json").read_text(encoding="utf-8"))
    assert manifest["grafana"]["crypto_only"] is True
    provider = (ROOT / "monitoring/grafana/provisioning/dashboards/v7.yml").read_text(encoding="utf-8")
    datasource = (ROOT / "monitoring/grafana/provisioning/datasources/prometheus-v7.yml").read_text(encoding="utf-8")
    assert "name: Polymarket V7 Crypto" in provider
    assert "folder: Polymarket London" in provider
    assert "folderUid: afyms0c1xjabkf" in provider
    assert "disableDeletion: false" in provider
    assert "allowUiUpdates: false" in provider
    assert "name: Polymarket V7 Crypto Prometheus" in datasource


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    assert tests
    for _, function in tests:
        function()
    print(f"{len(tests)} function tests passed")
