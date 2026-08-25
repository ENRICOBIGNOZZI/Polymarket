#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")
INTEGRATION_ONLY_LABELS = {
    "approved-for-integration",
    "single-model-reviewed",
    "administrator-approved",
    "autonomous-promotion-approved",
}
SOURCE_RESEARCH_PR_PATTERN = re.compile(r"source research pr/branch/commit:\s*#(\d+)\b", flags=re.IGNORECASE)
RESEARCH_VERDICT_PATTERN = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?Research Governance"
    r"(?:\s+(?:evidence state|correction|decision))?\s*(?:—|–|-|:)\s*"
    r"[`*_~]*(INTEGRATION_READY|APPROVED_FOR_INTEGRATION|MORE_EVIDENCE_REQUIRED|REJECTED|SHADOW_ONLY)\b"
)
APPROVED_RESEARCH_VERDICTS = {"INTEGRATION_READY", "APPROVED_FOR_INTEGRATION"}
TRUSTED_GOVERNANCE_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
MODEL_BODY_TERMS = ("alpha","model","strategy","signal","stat-arb","stat arb","pca","maker","opportunity","portfolio","paper champion","candidate bundle")
LIVE_MODEL_SURFACE_PATTERNS = (
    re.compile(r"^config/live_champion\.json$"), re.compile(r"^config/paper_v\d+\.json$"),
    re.compile(r"^config/v\d+_model_architecture\.json$"),
    re.compile(r"^config/(?:cross_venue|portfolio_supervisor)\.json$"), re.compile(r"^config/cross_venue_pairs\.csv$"),
    re.compile(r"^scripts/paper_v\d+_(?:loop|once)\.sh$"), re.compile(r"^scripts/multi_strategy_paper\.py$"),
    re.compile(r"^scripts/build_v\d+_intents\.py$"), re.compile(r"^scripts/merge_v\d+_intents\.py$"),
    re.compile(r"^scripts/build_global_opportunity_book\.py$"), re.compile(r"^scripts/filter_coherent_hedges\.py$"),
    re.compile(r"^scripts/walk_forward_v\d+\.py$"), re.compile(r"^scripts/runtime_action_report\.py$"),
    re.compile(r"^scripts/portfolio_supervisor\.py$"), re.compile(r"^scripts/(?:cross_venue_loop|prediction_market_system_loop)\.sh$"),
    re.compile(r"^scripts/v\d+_(?:dynamic_factor_intents|execution_model|external_bridge|global_risk|hard_arb_paper|intent_guard|intent_queue_filter|local_factor_intents|materialize_configs|micro_taker|micro_taker_institutional|queue_filter|relation_intents)\.py$"),
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


def author_association(item: dict[str, Any]) -> str:
    return str(item.get("authorAssociation") or item.get("author_association") or "").upper()


def research_verdict(source: dict[str, Any] | None) -> str | None:
    """Return the latest trusted, explicitly marked Research Governance verdict.

    Source PR prose is evidence supplied by the research author and is never an
    approval authority. Only a structured governance comment/review authored by
    an OWNER, MEMBER or COLLABORATOR can supply a verdict used by integration.
    """
    if not isinstance(source, dict):
        return None
    verdicts: list[tuple[str, int, int, str]] = []
    ordinal = 0
    for key, timestamp_keys in (
        ("comments", ("createdAt", "created_at")),
        ("reviews", ("submittedAt", "submitted_at", "createdAt", "created_at")),
    ):
        for item in source.get(key) or []:
            if not isinstance(item, dict):
                continue
            if author_association(item) not in TRUSTED_GOVERNANCE_AUTHOR_ASSOCIATIONS:
                continue
            timestamp = ""
            for timestamp_key in timestamp_keys:
                value = item.get(timestamp_key)
                if value:
                    timestamp = str(value)
                    break
            text = str(item.get("body") or "")
            for match_order, match in enumerate(RESEARCH_VERDICT_PATTERN.finditer(text)):
                verdicts.append((timestamp, ordinal, match_order, match.group(1).upper()))
            ordinal += 1
    if not verdicts:
        return None
    verdicts.sort(key=lambda item: (item[0], item[1], item[2]))
    return verdicts[-1][3]


def source_branch(source: dict[str, Any] | None) -> str:
    if not isinstance(source, dict):
        return ""
    direct = str(source.get("headRefName") or "")
    if direct:
        return direct
    head = source.get("head")
    return str(head.get("ref") or "") if isinstance(head, dict) else ""


def source_number(source: dict[str, Any] | None) -> int | None:
    if not isinstance(source, dict):
        return None
    try:
        return int(source.get("number"))
    except (TypeError, ValueError):
        return None


def evaluate(
    event: dict[str, Any],
    changed_files: set[str],
    manifest_existed_on_base: bool,
    source_research: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    pr = event.get("pull_request")
    if not isinstance(pr, dict): return ["event does not contain pull_request metadata"], {}
    head = str(pr.get("head", {}).get("ref") or pr.get("headRefName") or "")
    body = str(pr.get("body") or ""); draft = bool(pr.get("draft")); labels = label_names(pr)
    manifest_changed = "config/live_champion.json" in changed_files
    model_surface_files = sorted(path for path in changed_files if is_sensitive_model_surface(path))
    opaque_model_bootstrap = is_opaque_model_bootstrap(changed_files, body)
    forbidden_shadow_files = shadow_forbidden_files(changed_files) if "shadow-isolated" in labels else []
    errors: list[str] = []
    linked_source_match = SOURCE_RESEARCH_PR_PATTERN.search(body)
    linked_source_number = int(linked_source_match.group(1)) if linked_source_match else None
    latest_source_verdict = research_verdict(source_research)

    if head.startswith(RESEARCH_PREFIXES):
        forbidden = sorted(labels.intersection(INTEGRATION_ONLY_LABELS))
        if forbidden: errors.append("research/experiment/diagnostic PRs cannot carry integration/administrator labels: " + ", ".join(forbidden))
        if not draft and "shadow-isolated" not in labels:
            errors.append("research PRs that are not shadow-isolated must remain draft; promotion happens through an integration/* candidate after objective validation")
        if manifest_changed: errors.append("research and diagnostic branches may never change the live champion manifest")
    elif head.startswith("integration/"):
        if model_surface_files:
            if linked_source_number is None:
                errors.append("sensitive integration PR must link a numbered source research PR as `Source research PR/branch/commit: #<number>`")
            if source_research is None:
                errors.append("sensitive integration PRs must provide approved source research metadata before model/runtime work may move onto integration/*")
            else:
                actual_source_number = source_number(source_research)
                actual_source_branch = source_branch(source_research)
                if linked_source_number is not None and actual_source_number != linked_source_number:
                    errors.append("sensitive integration source research PR does not match the numbered source in the candidate body")
                if not actual_source_branch.startswith(RESEARCH_PREFIXES):
                    errors.append("sensitive integration source must come from research/*, experiment/*, or diagnostic/*")
                if latest_source_verdict not in APPROVED_RESEARCH_VERDICTS:
                    errors.append(
                        "unapproved model/runtime work must remain in research until the latest trusted Research Governance verdict is APPROVED_FOR_INTEGRATION or INTEGRATION_READY; "
                        f"latest trusted source verdict: {latest_source_verdict or 'none'}"
                    )
        elif not draft and linked_source_number is None:
            errors.append("integration PR must link a numbered source research PR as `Source research PR/branch/commit: #<number>`")
    else:
        misplaced = sorted(labels.intersection(INTEGRATION_ONLY_LABELS | {"research-approved"}))
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
        "source_research_pr": linked_source_number if linked_source_number is not None else "none",
        "source_research_verdict": latest_source_verdict or "none",
        "automatic_paper_promotion": head.startswith("integration/"), "manual_approval_labels_required": False,
        "policy": "pass" if not errors else "fail",
    }
    return errors, summary


def render(summary: dict[str, Any], errors: list[str]) -> str:
    lines = ["# Research pull-request policy", ""]
    for key in ("branch","draft","labels","manifest_changed","model_surface_files","opaque_model_bootstrap","shadow_forbidden_files","changed_files","source_research_pr","source_research_verdict","automatic_paper_promotion","manual_approval_labels_required","policy"):
        value = summary.get(key, "unknown")
        if isinstance(value, list): value = ", ".join(str(item) for item in value) or "none"
        lines.append(f"- {key}: `{value}`")
    if errors: lines.extend(["", "## Policy errors"]); lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce Polymarket research/integration PR policy")
    parser.add_argument("--event", required=True); parser.add_argument("--changed-files", required=True)
    parser.add_argument("--manifest-existed-on-base", choices=("true", "false"), required=True); parser.add_argument("--output", required=True)
    parser.add_argument("--source-research-json")
    args = parser.parse_args()

    source_research = None
    if args.source_research_json:
        source_research = json.loads(Path(args.source_research_json).read_text(encoding="utf-8"))

    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    changed = {line.strip() for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines() if line.strip()}
    errors, summary = evaluate(
        event,
        changed,
        manifest_existed_on_base=args.manifest_existed_on_base == "true",
        source_research=source_research,
    )
    report = render(summary, errors); Path(args.output).write_text(report, encoding="utf-8"); print(report, end="")
    for error in errors: print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
