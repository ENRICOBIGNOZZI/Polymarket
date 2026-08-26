#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable

RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")
INTEGRATION_ONLY_LABELS = {"autonomous-promotion-approved"}
APPROVED_RESEARCH_VERDICTS = {"APPROVED_FOR_INTEGRATION", "APPROVED_FOR_PAPER_PROMOTION"}
RESEARCH_VERDICTS = APPROVED_RESEARCH_VERDICTS | {"MORE_EVIDENCE_REQUIRED", "REJECTED"}
TRUSTED_VERDICT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
VERDICT_MARKER_RE = re.compile(
    r"Research Governance(?: correction)?\s*(?:—|:|-)\s*(APPROVED_FOR_INTEGRATION|APPROVED_FOR_PAPER_PROMOTION|MORE_EVIDENCE_REQUIRED|REJECTED)\b",
    re.IGNORECASE,
)
VERDICT_EXACT_SHA_RE = re.compile(r"Exact validated head:\s*`?([0-9a-fA-F]{40})`?", re.IGNORECASE)
SOURCE_PROVENANCE_RE = re.compile(
    r"Source research PR/branch/commit:\s*#(\d+)\s*/\s*`?([^\s/`]+/[^\s`]+)`?\s*/\s*`?([0-9a-fA-F]{40})`?",
    re.IGNORECASE,
)

LIVE_MODEL_SURFACE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^config/live_champion\.json$",
        r"^config/paper_v7\.json$",
        r"^config/research_v7_.*\.json$",
        r"^config/v7_frequency_matrix\.json$",
        r"^config/v7_execution_evidence\.json$",
        r"^scripts/paper_v7_(?:loop|execution_loop)\.sh$",
        r"^scripts/v7_.*\.py$",
        r"^scripts/runtime_action_report\.py$",
        r"^scripts/runtime_plane_supervisor\.py$",
        r"^scripts/runtime_singleton_launcher\.py$",
        r"^scripts/polymarket_fees\.py$",
        r"^src/(?:engine|maker_paper|multileg_paper|negrisk_arb|pca_stat_arb|stat_arb|fast_arb|fast_arb_main)\.cpp$",
        r"^include/pm/(?:engine|fast_arb|market_relation|execution|trade_identity)\.hpp$",
    )
)
OPERATOR_AUTHORITY_SURFACE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^config/operator_directives\.json$",
        r"^scripts/hard_safety_policy\.py$",
        r"^tests/test_v7_authorized_paper_envelope\.py$",
        r"^\.github/workflows/operator-authority-gate\.yml$",
    )
)
OPERATOR_AUTHORIZATION_MARKER = "Operator authorization change: latest explicit user instruction"
SHADOW_FORBIDDEN_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^config/live_champion\.json$",
        r"^config/paper_v7\.json$",
        r"^scripts/paper_v7_(?:loop|execution_loop)\.sh$",
        r"^scripts/runtime_action_report\.py$",
        r"^scripts/runtime_plane_supervisor\.py$",
        r"^scripts/runtime_singleton_launcher\.py$",
        r"^scripts/v7_(?:multileg_broker|multileg_broker_runner|micro_maker_worker|micro_taker_worker|hard_arb_execution|hard_arb_guard|runtime_status|capacity_lock|merge_intents|intent_guard|bundle_quote_optimizer|relation_intents|external_bridge)\.py$",
        r"^src/(?:engine|maker_paper|multileg_paper|negrisk_arb|pca_stat_arb|stat_arb|fast_arb|fast_arb_main)\.cpp$",
        r"^include/pm/(?:engine|fast_arb|market_relation|execution|trade_identity)\.hpp$",
    )
)
OPAQUE_BOOTSTRAP_SUFFIXES = (".b64", ".base64", ".enc", ".blob")


def read_event(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_changed_files(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def branch_kind(head: str) -> str:
    if head == "main":
        return "main"
    if head.startswith("integration/"):
        return "integration"
    if head.startswith("operator/"):
        return "operator"
    if head.startswith(RESEARCH_PREFIXES):
        return "research"
    return "feature"


def matches_any(path: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(pattern.search(path) for pattern in patterns)


def model_surface_files(changed: set[str]) -> list[str]:
    return sorted(path for path in changed if matches_any(path, LIVE_MODEL_SURFACE_PATTERNS))


def operator_authority_files(changed: set[str]) -> list[str]:
    return sorted(path for path in changed if matches_any(path, OPERATOR_AUTHORITY_SURFACE_PATTERNS))


def shadow_forbidden_files(changed: set[str]) -> list[str]:
    return sorted(path for path in changed if matches_any(path, SHADOW_FORBIDDEN_PATTERNS))


def opaque_bootstrap_files(changed: set[str]) -> list[str]:
    out: list[str] = []
    for path in changed:
        lower = path.lower()
        name = Path(lower).name
        if lower.endswith(OPAQUE_BOOTSTRAP_SUFFIXES):
            out.append(path)
            continue
        if any(token in name for token in ("payload", "bootstrap", "patch")) and ("alpha" in lower or "model" in lower or "paper" in lower):
            out.append(path)
    return sorted(out)


def labels(pr: dict[str, Any]) -> set[str]:
    values = pr.get("labels", [])
    result: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            name = str(value.get("name", "")).strip()
        else:
            name = str(value).strip()
        if name:
            result.add(name)
    return result


def parse_source_provenance(body: str) -> tuple[int, str, str] | None:
    match = SOURCE_PROVENANCE_RE.search(body or "")
    if not match:
        return None
    return int(match.group(1)), match.group(2), match.group(3).lower()


def _source_head(source: dict[str, Any]) -> tuple[str, str]:
    branch = str(source.get("headRefName") or "")
    sha = str(source.get("headRefOid") or "")
    if branch and sha:
        return branch, sha.lower()
    head = source.get("head") if isinstance(source.get("head"), dict) else {}
    return str(head.get("ref") or ""), str(head.get("sha") or "").lower()


def _comment_fields(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("createdAt") or row.get("created_at") or ""),
        str(row.get("authorAssociation") or row.get("author_association") or "").upper(),
        str(row.get("body") or ""),
    )


def _review_fields(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("submittedAt") or row.get("submitted_at") or ""),
        str(row.get("authorAssociation") or row.get("author_association") or "").upper(),
        str(row.get("body") or ""),
    )


def _trusted_verdict_candidates(source: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    candidates: list[tuple[str, str, str, str]] = []
    for row in source.get("comments", []) if isinstance(source.get("comments"), list) else []:
        if not isinstance(row, dict):
            continue
        created, association, body = _comment_fields(row)
        if association not in TRUSTED_VERDICT_ASSOCIATIONS:
            continue
        marker = VERDICT_MARKER_RE.search(body)
        if marker:
            candidates.append((created, "comment", marker.group(1).upper(), body))
    for row in source.get("reviews", []) if isinstance(source.get("reviews"), list) else []:
        if not isinstance(row, dict):
            continue
        created, association, body = _review_fields(row)
        if association not in TRUSTED_VERDICT_ASSOCIATIONS:
            continue
        marker = VERDICT_MARKER_RE.search(body)
        if marker:
            candidates.append((created, "review", marker.group(1).upper(), body))
    return sorted(candidates, key=lambda item: (item[0], item[1]))


def extract_source_verdict(source: dict[str, Any]) -> tuple[str, str, str]:
    candidates = _trusted_verdict_candidates(source)
    if not candidates:
        return "", "", ""
    _, source_kind, verdict, body = candidates[-1]
    sha_match = VERDICT_EXACT_SHA_RE.search(body)
    approved_sha = sha_match.group(1).lower() if sha_match else ""
    return verdict, approved_sha, source_kind


def evaluate_source_research(source: dict[str, Any], provenance: tuple[int, str, str] | None) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    verdict, approved_sha, verdict_source = extract_source_verdict(source)
    source_number = int(source.get("number") or 0)
    source_branch, source_head_sha = _source_head(source)
    summary = {
        "source_research_number": source_number,
        "source_research_branch": source_branch,
        "source_research_head_sha": source_head_sha,
        "source_research_verdict": verdict,
        "source_research_approved_sha": approved_sha,
        "source_research_verdict_source": verdict_source,
    }
    if provenance is None:
        errors.append("integration PR must bind exact source provenance: Source research PR/branch/commit: #<n> / research/<branch> / <40-char-sha>")
        return errors, summary
    number, branch, sha = provenance
    if source_number and source_number != number:
        errors.append(f"integration source PR mismatch: body cites #{number} but source metadata is #{source_number}")
    if source_branch and source_branch != branch:
        errors.append(f"integration source branch mismatch: body cites {branch} but source metadata is {source_branch}")
    if source_head_sha and source_head_sha != sha:
        errors.append(f"integration source commit mismatch: body cites {sha} but source head is {source_head_sha}")
    if not verdict:
        errors.append("source research PR has no trusted structured governance verdict; latest trusted source verdict: none")
    elif verdict not in APPROVED_RESEARCH_VERDICTS:
        errors.append(f"source research PR is not approved for integration; latest trusted source verdict: {verdict}")
    else:
        if not approved_sha:
            errors.append("positive source research verdict must bind an exact validated source head SHA")
        elif source_head_sha and approved_sha != source_head_sha:
            errors.append(f"source research changed after approval: approved {approved_sha}, current head {source_head_sha}")
        elif approved_sha != sha:
            errors.append(f"integration source commit mismatch: body cites {sha} but approved source head is {approved_sha}")
    return errors, summary


def evaluate(
    event: dict[str, Any],
    changed: set[str],
    manifest_existed_on_base: bool,
    source_research: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    pr = event.get("pull_request", {}) if isinstance(event, dict) else {}
    head = str(pr.get("head", {}).get("ref", ""))
    draft = bool(pr.get("draft", False))
    body = str(pr.get("body") or "")
    label_names = labels(pr)
    kind = branch_kind(head)
    errors: list[str] = []
    models = model_surface_files(changed)
    authority = operator_authority_files(changed)
    opaque = opaque_bootstrap_files(changed)
    shadow_forbidden = shadow_forbidden_files(changed)
    manifest_changed = "config/live_champion.json" in changed
    operator_marker = OPERATOR_AUTHORIZATION_MARKER in body

    if kind == "research" and not draft:
        errors.append("unapproved research/experiment/diagnostic work must remain draft until integrated through the approved V7 lifecycle")
    if kind not in {"research", "integration"} and models:
        errors.append("unapproved model/runtime work cannot change live V7 model surfaces outside research/* or integration/*: " + ", ".join(models))
    if kind not in {"research", "integration"} and opaque:
        errors.append("opaque bootstrap payload cannot hide model/runtime work outside research/* or integration/*: " + ", ".join(opaque))
    if "shadow-isolated" in label_names and shadow_forbidden:
        errors.append("shadow-isolated code cannot modify production intents, PnL, risk or execution surfaces: " + ", ".join(shadow_forbidden))
    if kind == "research" and manifest_changed:
        errors.append("research branches must not change config/live_champion.json")
    if kind != "operator" and authority:
        errors.append("operator authority surfaces may change only on operator/* with the latest explicit user instruction: " + ", ".join(authority))
    if kind == "operator" and manifest_changed:
        errors.append("operator authority PRs may not change the live champion manifest; use the V7 integration/promotion path")
    if kind == "operator" and authority and not operator_marker:
        errors.append(f"operator authority changes must contain the exact marker: {OPERATOR_AUTHORIZATION_MARKER}")

    integration_only_found = sorted(INTEGRATION_ONLY_LABELS.intersection(label_names))
    if kind != "integration" and integration_only_found:
        errors.append("integration-only machine promotion labels are invalid on non-integration branches: " + ", ".join(integration_only_found))

    source_provenance = parse_source_provenance(body) if kind == "integration" else None
    source_summary: dict[str, Any] = {
        "source_research_number": 0,
        "source_research_branch": "",
        "source_research_head_sha": "",
        "source_research_verdict": "",
        "source_research_approved_sha": "",
        "source_research_verdict_source": "",
    }
    if kind == "integration" and not draft:
        if source_provenance is None:
            errors.append("non-draft integration PR must bind exact source provenance: Source research PR/branch/commit: #<n> / research/<branch> / <40-char-sha>")
        elif source_research is None:
            errors.append("non-draft integration PR is missing source research metadata for exact approval verification")
        else:
            source_errors, source_summary = evaluate_source_research(source_research, source_provenance)
            errors.extend(source_errors)
    elif kind == "integration" and source_research is not None:
        source_errors, source_summary = evaluate_source_research(source_research, source_provenance)
        errors.extend(source_errors)

    if manifest_changed and not manifest_existed_on_base and kind != "integration":
        errors.append("initial live champion creation must occur on integration/*")

    integrated_from_approved_research = (
        kind == "integration"
        and source_summary.get("source_research_verdict") in APPROVED_RESEARCH_VERDICTS
        and bool(source_summary.get("source_research_approved_sha"))
        and not any("source research" in error.lower() or "integration source" in error.lower() for error in errors)
    )
    summary = {
        "head": head,
        "kind": kind,
        "draft": draft,
        "labels": sorted(label_names),
        "changed_file_count": len(changed),
        "model_surface_files": models,
        "operator_authority_files": authority,
        "opaque_bootstrap_files": opaque,
        "shadow_forbidden_files": shadow_forbidden,
        "manifest_changed": manifest_changed,
        "manifest_existed_on_base": manifest_existed_on_base,
        "operator_authorization_marker": operator_marker,
        "source_provenance": source_provenance,
        "integrated_from_approved_research": integrated_from_approved_research,
        "automatic_paper_promotion": kind == "integration",
        "manual_approval_labels_required": False,
        **source_summary,
        "policy": "pass" if not errors else "fail",
    }
    return errors, summary


def render(errors: list[str], summary: dict[str, Any]) -> str:
    lines = [
        "# Research PR policy", "",
        f"- head: `{summary['head']}`",
        f"- kind: `{summary['kind']}`",
        f"- draft: `{summary['draft']}`",
        f"- labels: `{', '.join(summary['labels']) or 'none'}`",
        f"- changed files: `{summary['changed_file_count']}`",
        f"- model/runtime surfaces changed: `{len(summary['model_surface_files'])}`",
        f"- operator authority surfaces changed: `{len(summary['operator_authority_files'])}`",
        f"- opaque bootstrap surfaces changed: `{len(summary['opaque_bootstrap_files'])}`",
        f"- shadow-forbidden surfaces changed: `{len(summary['shadow_forbidden_files'])}`",
        f"- live champion changed: `{summary['manifest_changed']}`",
        f"- live champion existed on base: `{summary['manifest_existed_on_base']}`",
        f"- operator_authorization_marker: `{summary['operator_authorization_marker']}`",
        f"- integrated from approved research: `{summary['integrated_from_approved_research']}`",
        f"- automatic_paper_promotion: `{summary['automatic_paper_promotion']}`",
        f"- manual_approval_labels_required: `{summary['manual_approval_labels_required']}`",
    ]
    if summary.get("source_provenance"):
        lines.append(f"- source provenance: `{summary['source_provenance']}`")
    lines.extend(
        [
            f"- source_research_verdict: `{summary.get('source_research_verdict') or 'none'}`",
            f"- source_research_head_sha: `{summary.get('source_research_head_sha') or 'none'}`",
            f"- source_research_approved_sha: `{summary.get('source_research_approved_sha') or 'none'}`",
            f"- source_research_verdict_source: `{summary.get('source_research_verdict_source') or 'none'}`",
        ]
    )
    if summary["model_surface_files"]:
        lines.extend(["", "## V7 model/runtime surfaces"]); lines.extend(f"- `{path}`" for path in summary["model_surface_files"])
    if summary["operator_authority_files"]:
        lines.extend(["", "## Operator authority surfaces"]); lines.extend(f"- `{path}`" for path in summary["operator_authority_files"])
    if summary["opaque_bootstrap_files"]:
        lines.extend(["", "## Opaque bootstrap surfaces"]); lines.extend(f"- `{path}`" for path in summary["opaque_bootstrap_files"])
    if summary["shadow_forbidden_files"]:
        lines.extend(["", "## Shadow-forbidden surfaces"]); lines.extend(f"- `{path}`" for path in summary["shadow_forbidden_files"])
    lines.extend(["", "## Result", f"- policy: `{summary['policy']}`"])
    if errors:
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Enforce V7 research/integration/authority PR policy")
    ap.add_argument("--event", type=Path, required=True)
    ap.add_argument("--changed-files", type=Path, required=True)
    ap.add_argument("--manifest-existed-on-base", choices=("true", "false"), required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--source-research-json", type=Path)
    args = ap.parse_args()
    event = read_event(args.event)
    changed = read_changed_files(args.changed_files)
    source_research = read_event(args.source_research_json) if args.source_research_json and args.source_research_json.exists() else None
    errors, summary = evaluate(event, changed, args.manifest_existed_on_base == "true", source_research)
    report = render(errors, summary)
    args.output.write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
