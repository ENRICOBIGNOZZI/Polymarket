#!/usr/bin/env python3
"""V7 Maker champion/challenger registry and explicit PAPER promotion gate.

Refitting and promotion are intentionally separate. Registering a challenger can
never change the runtime champion. Promotion requires a separately produced
chronological OOS + common-sample shadow-PAPER validation report and an explicit
operator approval token. This tool never enables authenticated or REAL trading.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REGISTRY_SCHEMA = "polymarket_v7_maker_model_registry_v1"
MODEL_SCHEMA = "polymarket_v7_maker_execution_model_v1"
STRATEGY = "MICRO_MAKER_PRO"


class RegistryError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RegistryError(f"invalid_json:{path}") from exc
    if not isinstance(value, dict):
        raise RegistryError(f"not_object:{path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_sha(value: Any) -> str:
    text = str(value or "")
    if not _SHA_RE.fullmatch(text):
        raise RegistryError("model_sha:not_exact_git_sha")
    return text


def _validate_model(model: dict[str, Any], expected_sha: str, *, role: str | None = None) -> None:
    if model.get("schema") != MODEL_SCHEMA:
        raise RegistryError("model:schema")
    if model.get("strategy") != STRATEGY:
        raise RegistryError("model:strategy")
    if model.get("paper_only") is not True:
        raise RegistryError("model:not_paper_only")
    if model.get("authenticated_execution") is not False:
        raise RegistryError("model:authenticated_execution")
    if model.get("real_order_submission", False) is not False:
        raise RegistryError("model:real_order_submission")
    if _exact_sha(model.get("model_sha")) != expected_sha:
        raise RegistryError("model:mixed_sha")
    if _exact_sha(model.get("code_sha", expected_sha)) != expected_sha:
        raise RegistryError("model:mixed_code_sha")
    if not isinstance(model.get("groups"), dict) or "GLOBAL" not in model["groups"]:
        raise RegistryError("model:missing_global_group")
    if role is not None and model.get("artifact_role") != role:
        raise RegistryError(f"model:expected_{role}")


def _empty_registry(model_sha: str) -> dict[str, Any]:
    return {
        "schema": REGISTRY_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "champion": None,
        "challenger": None,
        "history": [],
    }


def _load_registry(path: Path, model_sha: str) -> dict[str, Any]:
    if not path.exists():
        return _empty_registry(model_sha)
    registry = _read_json(path)
    if registry.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError("registry:schema")
    if registry.get("paper_only") is not True or registry.get("authenticated_execution") is not False:
        raise RegistryError("registry:safety")
    if registry.get("real_order_submission", False) is not False:
        raise RegistryError("registry:real_order_submission")
    if _exact_sha(registry.get("model_sha")) != model_sha:
        raise RegistryError("registry:mixed_sha")
    if not isinstance(registry.get("history"), list):
        raise RegistryError("registry:history")
    return registry


def _model_entry(path: Path, model: dict[str, Any], status: str) -> dict[str, Any]:
    training = model.get("training_window") if isinstance(model.get("training_window"), dict) else {}
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "family": model.get("family"),
        "version": model.get("version", model.get("generated_ts_ms")),
        "feature_schema": model.get("feature_schema"),
        "training_end_ts_ms": training.get("end_ts_ms"),
        "training_window": training,
        "hyperparameters": model.get("hyperparameters"),
        "policy_version": model.get("policy_version"),
        "code_sha": model.get("code_sha"),
        "registered_ts_ms": time.time_ns() // 1_000_000,
        "status": status,
    }


def register_challenger(*, challenger: Path, registry_path: Path,
                        model_sha: str, champion: Path | None = None) -> dict[str, Any]:
    model_sha = _exact_sha(model_sha)
    challenger_model = _read_json(challenger)
    _validate_model(challenger_model, model_sha, role="challenger")
    if challenger_model.get("promotion_state") != "CHALLENGER_PENDING_OOS":
        raise RegistryError("challenger:promotion_state")
    if challenger_model.get("eligible_for_live_reload") is not False:
        raise RegistryError("challenger:live_reload_must_be_false")

    registry = _load_registry(registry_path, model_sha)
    registry["challenger"] = _model_entry(challenger, challenger_model, "PENDING_OOS")
    if champion is not None and champion.exists():
        champion_model = _read_json(champion)
        # Backward-compatible champions created before this registry may not yet
        # carry artifact_role. They are accepted only if all safety/SHA contracts
        # hold; the registry marks them as the incumbent rather than mutating them.
        _validate_model(champion_model, model_sha)
        registry["champion"] = _model_entry(champion, champion_model, "CHAMPION")
    elif registry.get("champion") is None:
        registry["champion"] = {
            "status": "STATIC_POLICY_BASELINE",
            "path": str(champion) if champion is not None else None,
            "code_sha": model_sha,
        }

    history = registry["history"]
    history.append({
        "ts_ms": time.time_ns() // 1_000_000,
        "action": "REGISTER_CHALLENGER",
        "challenger_sha256": registry["challenger"]["sha256"],
    })
    registry["history"] = history[-200:]
    _atomic_json(registry_path, registry)
    return registry


def _validate_promotion_report(report: dict[str, Any], *, model_sha: str,
                               challenger_sha256: str,
                               min_event_clusters: int) -> None:
    if _exact_sha(report.get("model_sha")) != model_sha:
        raise RegistryError("validation:mixed_sha")
    if report.get("paper_only") is not True or report.get("authenticated_execution") is not False:
        raise RegistryError("validation:safety")
    if report.get("challenger_sha256") != challenger_sha256:
        raise RegistryError("validation:wrong_challenger")
    if report.get("status") != "PASS":
        raise RegistryError("validation:not_pass")
    required_true = (
        "chronological_oos",
        "common_sample",
        "shadow_paper",
        "robust_ev_positive",
        "execution_healthy",
        "latency_healthy",
        "inventory_controlled",
        "queue_credible",
        "fill_calibrated",
        "markout_calibrated",
    )
    for key in required_true:
        if report.get(key) is not True:
            raise RegistryError(f"validation:{key}")
    if int(report.get("event_clusters") or 0) < max(1, min_event_clusters):
        raise RegistryError("validation:event_clusters")
    try:
        improvement = float(report.get("oos_policy_improvement"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RegistryError("validation:oos_policy_improvement") from exc
    if not improvement > 0.0:
        raise RegistryError("validation:no_positive_oos_improvement")


def promote_challenger(*, challenger: Path, champion: Path, registry_path: Path,
                       validation_report: Path, model_sha: str,
                       operator_approval: str, min_event_clusters: int = 12) -> dict[str, Any]:
    model_sha = _exact_sha(model_sha)
    if not operator_approval.strip():
        raise RegistryError("promotion:operator_approval_required")

    challenger_model = _read_json(challenger)
    _validate_model(challenger_model, model_sha, role="challenger")
    challenger_sha = _sha256(challenger)
    report = _read_json(validation_report)
    _validate_promotion_report(
        report,
        model_sha=model_sha,
        challenger_sha256=challenger_sha,
        min_event_clusters=min_event_clusters,
    )

    registry = _load_registry(registry_path, model_sha)
    registered = registry.get("challenger")
    if not isinstance(registered, dict) or registered.get("sha256") != challenger_sha:
        raise RegistryError("promotion:challenger_not_registered")

    promoted = dict(challenger_model)
    promoted["artifact_role"] = "champion"
    promoted["promotion_state"] = "CHAMPION"
    promoted["eligible_for_live_reload"] = True
    promoted["promoted_ts_ms"] = time.time_ns() // 1_000_000
    promoted["promotion_validation_sha256"] = _sha256(validation_report)
    promoted["promotion_operator_approval"] = operator_approval.strip()
    _atomic_json(champion, promoted)

    registry["champion"] = _model_entry(champion, promoted, "CHAMPION")
    registered["status"] = "PROMOTED"
    registered["promoted_ts_ms"] = promoted["promoted_ts_ms"]
    history = registry["history"]
    history.append({
        "ts_ms": promoted["promoted_ts_ms"],
        "action": "PROMOTE_CHALLENGER",
        "challenger_sha256": challenger_sha,
        "validation_sha256": promoted["promotion_validation_sha256"],
        "operator_approval": operator_approval.strip(),
        "oos_policy_improvement": report.get("oos_policy_improvement"),
        "event_clusters": report.get("event_clusters"),
    })
    registry["history"] = history[-200:]
    _atomic_json(registry_path, registry)
    return registry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--challenger", type=Path, required=True)
    parser.add_argument("--champion", type=Path)
    parser.add_argument("--validation-report", type=Path)
    parser.add_argument("--operator-approval", default="")
    parser.add_argument("--min-event-clusters", type=int, default=12)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    if args.promote:
        if args.champion is None or args.validation_report is None:
            raise SystemExit("--promote requires --champion and --validation-report")
        registry = promote_challenger(
            challenger=args.challenger,
            champion=args.champion,
            registry_path=args.registry,
            validation_report=args.validation_report,
            model_sha=args.model_sha,
            operator_approval=args.operator_approval,
            min_event_clusters=args.min_event_clusters,
        )
    else:
        registry = register_challenger(
            challenger=args.challenger,
            registry_path=args.registry,
            model_sha=args.model_sha,
            champion=args.champion,
        )
    print(json.dumps(registry, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
