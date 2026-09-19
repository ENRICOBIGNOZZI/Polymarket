#!/usr/bin/env python3
"""Fail-closed validation for the canonical crypto-only Grafana surface."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

EXPECTED_DASHBOARD_KEYS = (
    "dashboard_file",
    "external_fair_dashboard",
    "latency_dashboard",
    "multi_crypto_dashboard",
)
FORBIDDEN_VISIBLE = (
    "two-engine",
    "arb events",
    "configured engines · not execution proof",
    "engine reporting source · configured is not running",
    "external-fair latency chain",
)


def fail(message: str) -> None:
    raise SystemExit(f"crypto Grafana contract failed: {message}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{path} is not a JSON object")
    return value


def walk_panels(dashboard: dict[str, Any]):
    stack = list(dashboard.get("panels") or [])
    while stack:
        panel = stack.pop()
        if not isinstance(panel, dict):
            continue
        yield panel
        stack.extend(panel.get("panels") or [])


def visible_text(dashboard: dict[str, Any]) -> str:
    chunks = [str(dashboard.get("title") or "")]
    chunks.extend(str(x) for x in dashboard.get("tags") or [])
    for link in dashboard.get("links") or []:
        if isinstance(link, dict):
            chunks.append(str(link.get("title") or ""))
    for panel in walk_panels(dashboard):
        chunks.extend((str(panel.get("title") or ""), str(panel.get("description") or "")))
        for target in panel.get("targets") or []:
            if isinstance(target, dict):
                chunks.append(str(target.get("legendFormat") or ""))
    return "\n".join(chunks)


def validate(root: Path) -> None:
    root = root.resolve()
    manifest = load_json(root / "monitoring/v7_monitoring_manifest.json")
    grafana = manifest.get("grafana") if isinstance(manifest.get("grafana"), dict) else {}
    if grafana.get("crypto_only") is not True:
        fail("monitoring manifest must declare grafana.crypto_only=true")
    expected_paths: list[Path] = []
    for key in EXPECTED_DASHBOARD_KEYS:
        rel = str(grafana.get(key) or "")
        if not rel.startswith("monitoring/grafana/dashboards/") or not rel.endswith(".json"):
            fail(f"invalid manifest dashboard path for {key}: {rel!r}")
        expected_paths.append(root / rel)
    if len({p.name for p in expected_paths}) != len(expected_paths):
        fail("dashboard manifest paths are not unique")

    dashboard_dir = root / "monitoring/grafana/dashboards"
    actual = {p.resolve() for p in dashboard_dir.glob("*.json") if p.is_file()}
    expected = {p.resolve() for p in expected_paths}
    if actual != expected:
        extra = sorted(p.name for p in actual - expected)
        missing = sorted(p.name for p in expected - actual)
        fail(f"dashboard inventory drift: extra={extra}, missing={missing}")

    dashboards = [load_json(p) for p in expected_paths]
    expected_uids = {str(v.get("uid") or "") for v in dashboards}
    if len(expected_uids) != len(dashboards) or "" in expected_uids:
        fail("dashboard UIDs must be unique and non-empty")
    if str(grafana.get("dashboard_uid") or "") not in expected_uids:
        fail("canonical operator dashboard UID is outside the crypto dashboard set")

    for path, dashboard in zip(expected_paths, dashboards):
        title = str(dashboard.get("title") or "")
        tags = {str(x).lower() for x in dashboard.get("tags") or []}
        if "crypto" not in title.lower() or "crypto" not in tags:
            fail(f"{path.name} is not explicitly crypto in title/tags")
        visible = visible_text(dashboard).lower()
        for token in FORBIDDEN_VISIBLE:
            if token in visible:
                fail(f"{path.name} contains retired visible UI token {token!r}")
        for link in dashboard.get("links") or []:
            if not isinstance(link, dict):
                continue
            if "crypto" not in str(link.get("title") or "").lower():
                fail(f"{path.name} has non-crypto navigation label")
            url = str(link.get("url") or "")
            if url.startswith("/d/") and url.removeprefix("/d/") not in expected_uids:
                fail(f"{path.name} links outside canonical crypto dashboard set: {url}")
        for panel in walk_panels(dashboard):
            for target in panel.get("targets") or []:
                if not isinstance(target, dict):
                    continue
                expr = str(target.get("expr") or "")
                if "polymarket_execution_arbs" in expr:
                    fail(f"{path.name} still renders retired generic arb-event telemetry")
                for retired_metric in ("polymarket_mc_coordinator_candidate_", "polymarket_mc_shadow_"):
                    if retired_metric in expr:
                        fail(f"{path.name} still renders retired metric family {retired_metric!r}")

    datasource = (root / "monitoring/grafana/provisioning/datasources/prometheus-v7.yml").read_text(encoding="utf-8")
    if "name: Polymarket V7 Crypto Prometheus" not in datasource:
        fail("Grafana datasource display name must be explicitly crypto")

    provider = (root / "monitoring/grafana/provisioning/dashboards/v7.yml").read_text(encoding="utf-8")
    if "name: Polymarket V7 Crypto" not in provider or "folder: Polymarket V7 Crypto" not in provider:
        fail("Grafana provider/folder must be explicitly crypto")
    if "disableDeletion: false" not in provider or "allowUiUpdates: false" not in provider:
        fail("Grafana provider must delete removed provisioned dashboards and reject UI edits")

    alerts = (root / "monitoring/v7_alerts.yml").read_text(encoding="utf-8").lower()
    if "polymarket_v7_live_algorithm_count != 1" not in alerts:
        fail("crypto-only live-algorithm alert must require exactly one live algorithm")
    for stale in ("two-engine", "polymarket_v7_live_algorithm_count != 2", "two canonical algorithms"):
        if stale in alerts:
            fail(f"alert catalog contains stale token {stale!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    validate(args.repository_root)
    print("crypto_grafana_contract=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
