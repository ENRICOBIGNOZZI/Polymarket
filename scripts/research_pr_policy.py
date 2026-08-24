#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")
LEGACY_INTEGRATION_LABELS = {"approved-for-integration", "single-model-reviewed", "administrator-approved"}
SOURCE_RESEARCH_PR_PATTERN = re.compile(r"source research pr/branch/commit:\s*#(\d+)\b", flags=re.IGNORECASE)
MODEL_BODY_TERMS = ("alpha","model","strategy","signal","stat-arb","stat arb","pca","maker","opportunity","portfolio","paper champion","candidate bundle")
LIVE_MODEL_SURFACE_PATTERNS = (
    re.compile(r"^config/live_champion\.json$"), re.compile(r"^config/paper_v\d+\.json$"),
    re.compile(r"^config/(?:cross_venue|portfolio_supervisor)\.json$"), re.compile(r"^config/cross_venue_pairs\.csv$"),
    re.compile(r"^scripts/paper_v\d+_(?:loop|once)\.sh$"), re.compile(r"^scripts/multi_strategy_paper\.py$"),
    re.compile(r"^scripts/build_v\d+_intents\.py$"), re.compile(r"^scripts/merge_v\d+_intents\.py$"),
    re.compile(r"^scripts/build_global_opportunity_book\.py$"), re.compile(r"^scripts/filter_coherent_hedges\.py$"),
    re.compile(r"^scripts/walk_forward_v\d+\.py$"), re.compile(r"^scripts/runtime_action_report\.py$"),
    re.compile(r"^scripts/portfolio_supervisor\.py$"), re.compile(r"^scripts/(?:cross_venue_loop|prediction_market_system_loop)\.sh$"),
)
MODEL_CODE_SURFACE_PATTERNS = (
    re.compile(r"^(?:src|include)(?:/.*)?/(?:engine|fast_arb|maker_paper|multileg_paper|negrisk_arb|pca_stat_arb|stat_arb|rewards_scan)\.(?:cpp|cc|cxx|h|hpp)$"),
    re.compile(r"^src/(?:engine|fast_arb|maker_paper|multileg_paper|negrisk_arb|pca_stat_arb|stat_arb|rewards_scan)\.(?:cpp|cc|cxx|h|hpp)$"),
    re.compile(r"^(?:src|include)(?:/.*)?/cross_venue(?:\.(?:cpp|cc|cxx|h|hpp)|_runtime/.*\.inc)$"),
)
SHADOW_FORBIDDEN_TOKENS = ("intent","broker","execution","risk","pnl","realized","drawdown","oos","kill","credential","auth","account","order","portfolio","supervisor","allocation","allocator","capital","sizing","position","exposure","wallet","secret","private_key","submit")


def label_names(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name")) for item in pr.get("labels", []) if item.get("name")}


def has_model_intent(body: str) -> bool:
    normalized = body.lower(); return any(term in normalized for term in MODEL_BODY_TERMS)


def is_live_model_surface(path: str) -> bool:
    return any(pattern.search(path) for pattern in LIVE_MODEL_SURFACE_PATTERNS)


def is_model_code_surface(path: str) -> bool:
    return any(pattern.search(path) for pattern in MODEL_CODE_SURFACE_PATTERNS)


def is_sensitive_model_surface(path: str) -> bool:
    return is_live_model_surface(path) or is_model_code_surface(path)


def is_opaque_model_bootstrap(changed_files: set[str], body: str) -> bool:
    bootstrap_workflow = any(path.startswith(".github/workflows/bootstrap-") and path.endswith((".yml", ".yaml")) for path in changed_files)
    opaque_payload = any(path.startswith("ops/") and path.endswith((".b64", ".tar.gz", ".tgz", ".zip")) for path in changed_files)
    return bootstrap_workflow and opaque_payload and has_model_intent(body)


def shadow_forbidden_files(changed_files: set[str]) -> list[str]:
    forbidden: list[str] = []
    for path in sorted(changed_files):
        lowered = path.lower()
        if is_sensitive_model_surface(path): forbidden.append(path); continue
        if not lowered.startswith(("config/", "scripts/", "src/", "include/", "ops/")): continue
        if any(token in lowered for token in SHADOW_FORBIDDEN_TOKENS): forbidden.append(path)
    return forbidden


def evaluate(event: dict[str, Any], changed_files: set[str], manifest_existed_on_base: bool) -> tuple[list[str], dict[str, Any]]:
    pr = event.get("pull_request")
    if not isinstance(pr, dict): return ["event does not contain pull_request metadata"], {}
    head = str(pr.get("head", {}).get("ref") or pr.get("headRefName") or "")
    body = str(pr.get("body") or ""); draft = bool(pr.get("draft")); labels = label_names(pr)
    manifest_changed = "config/live_champion.json" in changed_files
    model_surface_files = sorted(path for path in changed_files if is_sensitive_model_surface(path))
    opaque_model_bootstrap = is_opaque_model_bootstrap(changed_files, body)
    forbidden_shadow_files = shadow_forbidden_files(changed_files) if "shadow-isolated" in labels else []
    errors: list[str] = []

    if head.startswith(RESEARCH_PREFIXES):
        forbidden = sorted(labels.intersection(LEGACY_INTEGRATION_LABELS))
        if forbidden: errors.append("research/experiment/diagnostic PRs cannot carry integration/administrator labels: " + ", ".join(forbidden))
        if not draft and "shadow-isolated" not in labels:
            errors.append("research PRs that are not shadow-isolated must remain draft; promotion happens through an integration/* candidate after objective validation")
        if manifest_changed: errors.append("research and diagnostic branches may never change the live champion manifest")
    elif head.startswith("integration/"):
        if not draft and SOURCE_RESEARCH_PR_PATTERN.search(body) is None:
            errors.append("integration PR must link a numbered source research PR as `Source research PR/branch/commit: #<number>`")
    else:
        misplaced = sorted(labels.intersection(LEGACY_INTEGRATION_LABELS | {"research-approved"}))
        if misplaced: errors.append("research/integration labels are valid only on their dedicated branch classes: " + ", ".join(misplaced))
        if manifest_changed and manifest_existed_on_base: errors.append("an existing live champion manifest may change only on integration/*")
        if model_surface_files:
            errors.append("unapproved model/runtime work cannot change known live model/runtime/code surfaces on normal feature/fix branches; use research/*, experiment/*, or diagnostic/* for evidence and integration/* for automatic paper promotion. Sensitive change: " + ", ".join(model_surface_files))
        elif opaque_model_bootstrap:
            errors.append("opaque model/runtime bootstrap work must use research/*, experiment/*, or diagnostic/*; paper champion integration must use integration/*. Sensitive change: opaque bootstrap payload")

    if forbidden_shadow_files:
        errors.append("shadow-isolated code cannot modify production decision, model, execution, PnL, risk, OOS, credential, account, order, portfolio-allocation, sizing, exposure, or kill surfaces: " + ", ".join(forbidden_shadow_files))

    summary = {
        "branch": head, "draft": draft, "labels": sorted(labels), "manifest_changed": manifest_changed,
        "model_surface_files": model_surface_files, "opaque_model_bootstrap": opaque_model_bootstrap,
        "shadow_forbidden_files": forbidden_shadow_files, "changed_files": len(changed_files),
        "automatic_paper_promotion": head.startswith("integration/"), "manual_approval_labels_required": False,
        "policy": "pass" if not errors else "fail",
    }
    return errors, summary


def render(summary: dict[str, Any], errors: list[str]) -> str:
    lines = ["# Research pull-request policy", ""]
    for key in ("branch","draft","labels","manifest_changed","model_surface_files","opaque_model_bootstrap","shadow_forbidden_files","changed_files","automatic_paper_promotion","manual_approval_labels_required","policy"):
        value = summary.get(key, "unknown")
        if isinstance(value, list): value = ", ".join(str(item) for item in value) or "none"
        lines.append(f"- {key}: `{value}`")
    if errors: lines.extend(["", "## Policy errors"]); lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce Polymarket research/integration PR policy")
    parser.add_argument("--event", required=True); parser.add_argument("--changed-files", required=True)
    parser.add_argument("--manifest-existed-on-base", choices=("true", "false"), required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args()
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    changed = {line.strip() for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines() if line.strip()}
    errors, summary = evaluate(event, changed, manifest_existed_on_base=args.manifest_existed_on_base == "true")
    report = render(summary, errors); Path(args.output).write_text(report, encoding="utf-8"); print(report, end="")
    for error in errors: print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
