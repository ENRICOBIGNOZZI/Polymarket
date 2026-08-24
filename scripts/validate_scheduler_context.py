#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

EXPECTED_STATE = {
    "market_microstructure",
    "contract_characteristics",
    "cross_market_information",
    "external_information",
}
EXPECTED_ENGINES = {
    "microstructure_fair_value",
    "pca_statistical_arbitrage",
    "event_graph_and_logical_consistency",
    "semantic_relative_value",
    "external_information",
}
EXPECTED_SEPARATION = [
    "probability_estimation",
    "trade_decision",
    "portfolio_construction_and_risk",
    "execution_and_reconciliation",
]
EXPECTED_SCHEDULERS = {
    "administrator-supervisor",
    "research-policy",
    "research-queue",
    "integration-merge",
    "post-merge-validation",
    "code-validation",
    "monitoring-validation",
    "live-paper-validation",
    "paper-server-deploy",
    "paper-server-health",
    "forward-maker-research",
    "alpha-factory",
    "meta-supervisor",
    "fast-arb-shadow-research",
    "arb-theory-research",
    "live-api-smoke",
}
ALLOWED_PROFILES = {
    "supervisor",
    "policy",
    "research",
    "integration",
    "validation",
    "remote",
    "api",
}


def require_object(
    root: dict[str, Any], key: str, errors: list[str]
) -> dict[str, Any]:
    value = root.get(key)
    if not isinstance(value, dict):
        errors.append(f"{key} must be an object")
        return {}
    return value


def require_string(
    root: dict[str, Any],
    key: str,
    errors: list[str],
    expected: str | None = None,
) -> str:
    value = root.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} must be a non-empty string")
        return ""
    if expected is not None and value != expected:
        errors.append(f"{key} must equal {expected!r}; found {value!r}")
    return value


def canonical_json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def validate_context(
    data: dict[str, Any],
    *,
    scheduler_id: str | None = None,
    server_host: str | None = None,
    server_user: str | None = None,
    server_port: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    require_string(data, "project", errors, "Polymarket")

    source = require_object(data, "source", errors)
    require_string(
        source,
        "title",
        errors,
        "A Universal Quantitative Architecture for Polymarket",
    )
    require_string(source, "date", errors, "2026-08-23")

    remote = require_object(data, "remote_runtime", errors)
    require_string(
        remote, "access_path", errors, "GitHub Actions -> Tailscale -> SSH"
    )
    require_string(remote, "host_variable", errors, "POLYMARKET_SERVER_HOST")
    require_string(remote, "default_host", errors, "100.104.183.109")
    require_string(remote, "user_variable", errors, "POLYMARKET_SERVER_USER")
    require_string(remote, "default_user", errors, "enrico")
    if remote.get("default_port") != 22:
        errors.append("remote_runtime.default_port must equal 22")
    require_string(remote, "port_variable", errors, "POLYMARKET_SERVER_PORT")
    require_string(
        remote, "home_relative_repository_path", errors, "polymarket"
    )
    require_string(
        remote, "ssh_key_secret", errors, "POLYMARKET_SERVER_SSH_KEY"
    )
    require_string(remote, "tailscale_auth_secret", errors, "TS_AUTHKEY")
    require_string(remote, "deployment_ref", errors, "paper-validated")
    require_string(
        remote, "live_champion_manifest", errors, "config/live_champion.json"
    )
    if remote.get("server_alive_interval_seconds") != 30:
        errors.append("remote_runtime.server_alive_interval_seconds must equal 30")
    if remote.get("server_alive_count_max") != 3:
        errors.append("remote_runtime.server_alive_count_max must equal 3")
    if remote.get("batch_mode") is not True:
        errors.append("remote_runtime.batch_mode must be true")
    if server_host is not None and not server_host.strip():
        errors.append("resolved server host is empty")
    if server_user is not None and not server_user.strip():
        errors.append("resolved server user is empty")
    if server_port is not None:
        try:
            parsed_port = int(server_port)
        except ValueError:
            errors.append(f"resolved server port is not an integer: {server_port!r}")
        else:
            if parsed_port <= 0 or parsed_port > 65535:
                errors.append(f"resolved server port is outside 1..65535: {parsed_port}")

    architecture = require_object(data, "quantitative_architecture", errors)
    require_string(
        architecture,
        "objective",
        errors,
        "Category-agnostic quantitative operation over all sufficiently liquid Polymarket markets.",
    )
    if set(architecture.get("universal_state", [])) != EXPECTED_STATE:
        errors.append(
            "quantitative_architecture.universal_state must contain the four canonical state blocks"
        )
    if set(architecture.get("alpha_engines", [])) != EXPECTED_ENGINES:
        errors.append(
            "quantitative_architecture.alpha_engines must contain the five canonical experts"
        )
    if architecture.get("separation") != EXPECTED_SEPARATION:
        errors.append(
            "quantitative_architecture.separation must preserve estimation, decision, risk and execution"
        )
    ensemble = require_object(architecture, "ensemble", errors)
    require_string(ensemble, "type", errors, "adaptive_mixture_of_experts")
    if set(ensemble.get("outputs", [])) != {"fair_probability", "uncertainty"}:
        errors.append("ensemble outputs must be fair_probability and uncertainty")
    decision = require_object(architecture, "decision_contract", errors)
    for field in ("raw_alpha", "net_edge", "rank_by", "spread_rule"):
        require_string(decision, field, errors)
    risk = require_object(architecture, "portfolio_and_risk", errors)
    if risk.get("maximum_drawdown_ratio") != 0.15:
        errors.append("portfolio_and_risk.maximum_drawdown_ratio must equal 0.15")
    for flag in (
        "fractional_kelly",
        "gross_exposure_limit",
        "market_concentration_limit",
        "event_concentration_limit",
        "open_loss_budget",
        "drawdown_is_not_a_mathematical_guarantee",
    ):
        if risk.get(flag) is not True:
            errors.append(f"portfolio_and_risk.{flag} must be true")
    mode = require_object(architecture, "operating_mode", errors)
    require_string(mode, "research_engine", errors, "live-data paper and shadow")
    if mode.get("authenticated_order_submission") is not False:
        errors.append("operating_mode.authenticated_order_submission must be false")
    if mode.get("real_money_requires_separate_approval") is not True:
        errors.append("operating_mode.real_money_requires_separate_approval must be true")

    contract = require_object(data, "scheduler_contract", errors)
    profiles = require_object(contract, "profiles", errors)
    assignments = require_object(contract, "assignments", errors)
    if set(assignments) != EXPECTED_SCHEDULERS:
        missing = sorted(EXPECTED_SCHEDULERS.difference(assignments))
        extra = sorted(set(assignments).difference(EXPECTED_SCHEDULERS))
        if missing:
            errors.append("scheduler_context missing assignments: " + ", ".join(missing))
        if extra:
            errors.append("scheduler_context has unknown assignments: " + ", ".join(extra))
    for item_id, profile in assignments.items():
        if profile not in ALLOWED_PROFILES:
            errors.append(
                f"scheduler {item_id} has unsupported context profile {profile!r}"
            )
        if profile not in profiles:
            errors.append(f"missing scheduler profile description: {profile}")
    if (
        assignments.get("paper-server-deploy") != "remote"
        or assignments.get("paper-server-health") != "remote"
    ):
        errors.append("deploy and health schedulers must use the remote profile")
    if assignments.get("alpha-factory") != "research":
        errors.append("alpha-factory must use the research profile")
    if assignments.get("meta-supervisor") != "supervisor":
        errors.append("meta-supervisor must use the supervisor profile")
    all_rules = contract.get("all")
    if (
        not isinstance(all_rules, list)
        or len(all_rules) < 6
        or not all(isinstance(item, str) and item for item in all_rules)
    ):
        errors.append("scheduler_contract.all must contain the shared fail-closed rules")
    if scheduler_id is not None and scheduler_id not in assignments:
        errors.append(f"scheduler id is not assigned in context: {scheduler_id}")

    serialized = canonical_json_bytes(data).decode("utf-8")
    for fragment in (
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "BEGIN PRIVATE KEY",
        "ghp_",
        "github_pat_",
    ):
        if fragment in serialized:
            errors.append(
                f"scheduler context contains forbidden credential material: {fragment}"
            )
    return errors


def load_context(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("scheduler context root must be an object")
    return data


def context_sha256(data: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()


def render_markdown(
    data: dict[str, Any], scheduler_id: str | None, errors: list[str]
) -> str:
    remote = data.get("remote_runtime", {}) if isinstance(data, dict) else {}
    architecture = (
        data.get("quantitative_architecture", {}) if isinstance(data, dict) else {}
    )
    contract = data.get("scheduler_contract", {}) if isinstance(data, dict) else {}
    assignments = contract.get("assignments", {}) if isinstance(contract, dict) else {}
    profile = assignments.get(scheduler_id, "not-selected") if scheduler_id else "not-selected"
    mode = (
        architecture.get("operating_mode", {})
        if isinstance(architecture.get("operating_mode"), dict)
        else {}
    )
    risk = (
        architecture.get("portfolio_and_risk", {})
        if isinstance(architecture.get("portfolio_and_risk"), dict)
        else {}
    )
    lines = [
        "# Shared scheduler context",
        "",
        f"- scheduler: `{scheduler_id or 'registry-wide'}`",
        f"- profile: `{profile}`",
        f"- context SHA256: `{context_sha256(data) if data else 'unavailable'}`",
        f"- remote access: `{remote.get('access_path', 'unavailable')}`",
        f"- remote target: `{remote.get('default_user', '?')}@{remote.get('default_host', '?')}:{remote.get('default_port', '?')}`",
        f"- remote repository: `$HOME/{remote.get('home_relative_repository_path', '?')}`",
        f"- deployment ref: `{remote.get('deployment_ref', '?')}`",
        f"- live champion manifest: `{remote.get('live_champion_manifest', '?')}`",
        f"- research mode: `{mode.get('research_engine', '?')}`",
        f"- maximum drawdown operating ratio: `{risk.get('maximum_drawdown_ratio', '?')}`",
        "",
        "The scheduler must preserve the universal state, five-expert architecture, executable-edge decision rule, portfolio/risk separation and paper-versus-authenticated-execution boundary.",
    ]
    if errors:
        lines.extend(["", "## Errors"] + [f"- {error}" for error in errors])
    else:
        lines.extend(["", "Context contract is valid."])
    return "\n".join(lines) + "\n"


def write_github_env(
    path: Path, data: dict[str, Any], scheduler_id: str | None
) -> None:
    remote = data["remote_runtime"]
    architecture = data["quantitative_architecture"]
    assignments = data["scheduler_contract"]["assignments"]
    values = {
        "POLYMARKET_SCHEDULER_ID": scheduler_id or "registry-wide",
        "POLYMARKET_SCHEDULER_CONTEXT_SHA": context_sha256(data),
        "POLYMARKET_SCHEDULER_CONTEXT_PROFILE": assignments.get(
            scheduler_id, "registry-wide"
        ),
        "POLYMARKET_REMOTE_REPO_REL": remote["home_relative_repository_path"],
        "POLYMARKET_DEPLOYMENT_REF": remote["deployment_ref"],
        "POLYMARKET_LIVE_CHAMPION_MANIFEST": remote["live_champion_manifest"],
        "POLYMARKET_REMOTE_HOST_DEFAULT": remote["default_host"],
        "POLYMARKET_REMOTE_USER_DEFAULT": remote["default_user"],
        "POLYMARKET_REMOTE_PORT_DEFAULT": str(remote["default_port"]),
        "POLYMARKET_RESEARCH_MODE": architecture["operating_mode"]["research_engine"],
        "POLYMARKET_MAX_DRAWDOWN_RATIO": str(
            architecture["portfolio_and_risk"]["maximum_drawdown_ratio"]
        ),
        "POLYMARKET_AUTHENTICATED_ORDER_SUBMISSION": "false",
    }
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and expose the shared Polymarket scheduler context"
    )
    parser.add_argument("--context", default="config/scheduler_context.json")
    parser.add_argument("--scheduler-id")
    parser.add_argument("--server-host")
    parser.add_argument("--server-user")
    parser.add_argument("--server-port")
    parser.add_argument("--output")
    parser.add_argument("--output-json")
    parser.add_argument("--github-env")
    args = parser.parse_args()
    try:
        data = load_context(Path(args.context))
        errors = validate_context(
            data,
            scheduler_id=args.scheduler_id,
            server_host=args.server_host,
            server_user=args.server_user,
            server_port=args.server_port,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        data, errors = {}, [str(exc)]
    markdown = render_markdown(data, args.scheduler_id, errors)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(markdown, encoding="utf-8")
    if args.output_json:
        snapshot = {
            "schema_version": 1,
            "scheduler_id": args.scheduler_id or "registry-wide",
            "profile": (
                data.get("scheduler_contract", {})
                .get("assignments", {})
                .get(args.scheduler_id, "registry-wide")
                if data
                else "invalid"
            ),
            "context_sha256": context_sha256(data) if data else None,
            "valid": not errors,
            "errors": errors,
            "remote_runtime": data.get("remote_runtime", {}) if data else {},
            "quantitative_architecture": (
                data.get("quantitative_architecture", {}) if data else {}
            ),
        }
        output_json = Path(args.output_json)
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.github_env and data and not errors:
        write_github_env(Path(args.github_env), data, args.scheduler_id)
    print(markdown, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
