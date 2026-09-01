#!/usr/bin/env python3
"""Generate and validate the complete V7 migration-surface classification.

The manifest is an audit snapshot.  It covers every tracked or intended-to-be-
tracked path, every branch/tag/remote-tracking ref, every checked-in schema and
workflow, and every long-lived process/runtime path encoded by the canonical
launcher.  Classification never depends on words such as ``old`` or
``legacy`` alone.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_surface_classification_v1"
CLASSES = frozenset({
    "KEEP_CANONICAL",
    "MERGE_INTO_CANONICAL",
    "KEEP_ZERO_AUTHORITY_RESEARCH",
    "KEEP_TEMPORARY_COMPATIBILITY",
    "ARCHIVE_HISTORY_ONLY",
    "DELETE_ACTIVE_LEGACY",
    "EXTERNAL_BLOCKER",
})
REQUIRED_FIELDS = frozenset({
    "surface_id", "path_or_ref", "object_type", "classification",
    "current_imports_callers_readers_writers", "economic_authority",
    "unique_behavior_data_contribution", "canonical_replacement",
    "migration_status", "validation_tests", "deletion_gate",
    "rollback_artifact_tag", "final_disposition",
    "disposition_commit",
})

RESEARCH_MARKERS = (
    "research_v7_", "cross_sectional", "local_factor", "pca_stat_arb",
    "wallet_intelligence", "wallet_dataset", "market_open", "osint",
    "sports_latency", "sports_collector", "cross_platform", "graph_rv",
    "micro_taker", "research_shadow", "slow_economic_shadow",
    "fair_value_research", "external_settlement_train",
    "external_settlement_validate", "external_settlement_dataset",
)
COMPONENT_MARKERS = (
    "market_maker", "maker_", "professional_market_maker", "hard_arb",
    "fast_structural", "external_fair", "external_execution",
    "external_kernel", "external_state", "external_ingress",
    "external_protocol", "external_replay", "external_tape",
    "external_ws", "crypto_settlement_engine",
)
TEMPORARY_PATHS: frozenset[str] = frozenset()
ARCHIVE_PATHS = frozenset({
    "AGENT_DIRECTIVE_V7_UNIFICATION_AND_LEGACY_ERADICATION.md",
    "CODEX_START_HERE_V7_UNIFICATION.md",
    "ops/run_codex_v7_unification.sh",
    "ops/run_v7_unification_agent.sh",
})


class ClassificationError(ValueError):
    pass


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True,
        stderr=subprocess.DEVNULL,
    )


def _paths(root: Path) -> list[str]:
    # Include intended additions so the generated snapshot covers its own
    # generator, test, and output before their first commit.
    return sorted(set(_git(
        root, "ls-files", "--cached", "--others", "--exclude-standard",
    ).splitlines()))


def _refs(root: Path) -> list[tuple[str, str]]:
    lines = _git(
        root, "for-each-ref", "--format=%(refname)%09%(objectname)",
        "refs/heads", "refs/remotes", "refs/tags",
    ).splitlines()
    return sorted(tuple(line.split("\t", 1)) for line in lines if "\t" in line)


def _classification_for_name(name: str) -> str:
    lowered = name.lower()
    if name in TEMPORARY_PATHS:
        return "KEEP_TEMPORARY_COMPATIBILITY"
    if name in ARCHIVE_PATHS or lowered.startswith("artifacts/v7_unification/phase_"):
        return "ARCHIVE_HISTORY_ONLY"
    if any(marker in lowered for marker in RESEARCH_MARKERS):
        return "KEEP_ZERO_AUTHORITY_RESEARCH"
    if any(marker in lowered for marker in COMPONENT_MARKERS):
        return "MERGE_INTO_CANONICAL"
    return "KEEP_CANONICAL"


def _authority(name: str, classification: str) -> dict[str, Any]:
    lowered = name.lower()
    if lowered.startswith(("tests/", "docs/", "artifacts/", "schemas/", ".github/")):
        return {"owner": None, "capabilities": [], "executable": False}
    if classification in {"KEEP_ZERO_AUTHORITY_RESEARCH", "ARCHIVE_HISTORY_ONLY"}:
        return {"owner": None, "capabilities": [], "executable": False}
    if classification == "KEEP_TEMPORARY_COMPATIBILITY":
        engine = (
            "CRYPTO_SETTLEMENT_ENGINE" if "external_fair" in lowered
            else "STRUCTURAL_ARB_ENGINE"
        )
        return {
            "owner": engine, "capabilities": ["temporary_candidate_adapter"],
            "executable": False,
        }
    owner_markers = (
        ("capital_allocator", "V7_CANONICAL_ALLOCATOR", ["allocate", "reserve"]),
        ("portfolio_guard", "V7_CANONICAL_RISK", ["risk", "kill"]),
        ("v7_oms", "V7_CANONICAL_OMS", ["order_lifecycle"]),
        ("ledger_spool", "V7_CANONICAL_LEDGER", ["canonical_ledger_write"]),
        ("opportunity", "V7_GLOBAL_PORTFOLIO_COORDINATOR", ["coordinate"]),
        ("cutover", "V7_OPERATOR_EXACT_SHA_PROMOTION", ["cutover"]),
        ("runtime_supervisor", "V7_EXACT_SHA_RUNTIME_IDENTITY", ["runtime_identity"]),
    )
    for marker, owner, capabilities in owner_markers:
        if marker in lowered:
            return {"owner": owner, "capabilities": capabilities, "executable": True}
    if classification == "MERGE_INTO_CANONICAL":
        owner = (
            "STRUCTURAL_ARB_ENGINE"
            if "hard_arb" in lowered or "fast_structural" in lowered
            else "CRYPTO_SETTLEMENT_ENGINE"
        )
        return {
            "owner": owner, "capabilities": ["engine_component"],
            "executable": False,
        }
    return {"owner": None, "capabilities": [], "executable": False}


def _replacement(name: str, classification: str) -> str | None:
    if classification == "KEEP_CANONICAL":
        return name
    if classification == "KEEP_ZERO_AUTHORITY_RESEARCH":
        return "config/v7_authority_registry.json#research_zero_authority_families"
    if classification == "KEEP_TEMPORARY_COMPATIBILITY":
        return "scripts/v7_opportunity.py"
    if classification == "MERGE_INTO_CANONICAL":
        lowered = name.lower()
        return (
            "config/v7_authority_registry.json#STRUCTURAL_ARB_ENGINE"
            if "hard_arb" in lowered or "fast_structural" in lowered
            else "config/v7_authority_registry.json#CRYPTO_SETTLEMENT_ENGINE"
        )
    if classification == "ARCHIVE_HISTORY_ONLY":
        return "artifacts/v7_unification/path_classification.json"
    if classification == "DELETE_ACTIVE_LEGACY":
        return "origin/main"
    return None


def _entry(
    *, surface_id: str, name: str, object_type: str, classification: str,
    references: list[str] | None = None, unique: str | None = None,
    migration_status: str | None = None,
) -> dict[str, Any]:
    if classification == "KEEP_TEMPORARY_COMPATIBILITY":
        deletion_gate = "PHASE_7_DECLARATIVE_PROCESS_CUTOVER_PROVEN"
        final = "delete_after_gate"
    elif classification == "MERGE_INTO_CANONICAL":
        deletion_gate = "UNIQUE_BEHAVIOR_EQUIVALENCE_AND_SINGLE_CONSUMER_PROVEN"
        final = "merge_then_delete_or_reclassify_canonical"
    elif classification == "DELETE_ACTIVE_LEGACY":
        deletion_gate = "UNIQUE_COMMIT_AND_ACTIVE_REFERENCE_SCAN_CLEAN"
        final = "delete_after_gate"
    elif classification == "ARCHIVE_HISTORY_ONLY":
        deletion_gate = "NONE_IMMUTABLE_AUDIT_OR_ROLLBACK_EVIDENCE"
        final = "retain_as_nonruntime_history"
    elif classification == "KEEP_ZERO_AUTHORITY_RESEARCH":
        deletion_gate = "DELETE_ONLY_IF_INFORMATION_VALUE_IS_SUPERSEDED"
        final = "retain_zero_authority"
    elif classification == "EXTERNAL_BLOCKER":
        deletion_gate = "EXTERNAL_OWNER_ACTION_COMPLETED"
        final = "resolve_externally"
    else:
        deletion_gate = "NONE_CANONICAL_SURFACE"
        final = "retain_canonical"
    return {
        "surface_id": surface_id,
        "path_or_ref": name,
        "object_type": object_type,
        "classification": classification,
        "current_imports_callers_readers_writers": {
            "static_references": sorted(set(references or [])),
            "analysis": "exact path/module references from tracked text; runtime linkage is separately enumerated",
        },
        "economic_authority": _authority(name, classification),
        "unique_behavior_data_contribution": unique or (
            "tracked repository behavior, evidence, test, configuration, or documentation"
        ),
        "canonical_replacement": _replacement(name, classification),
        "migration_status": migration_status or (
            "pending_gate" if classification in {
                "MERGE_INTO_CANONICAL", "KEEP_TEMPORARY_COMPATIBILITY",
                "DELETE_ACTIVE_LEGACY", "EXTERNAL_BLOCKER",
            } else "classified"
        ),
        "validation_tests": ["tests/test_v7_surface_classification.py"],
        "deletion_gate": deletion_gate,
        "rollback_artifact_tag": "v7-unification-pre-migration-8d9e8e60",
        "final_disposition": final,
        "disposition_commit": None,
    }


def _static_references(root: Path, paths: list[str]) -> dict[str, list[str]]:
    known = set(paths)
    module_paths: dict[str, str] = {}
    for path in paths:
        candidate = Path(path)
        if candidate.suffix == ".py":
            module_paths[candidate.stem] = path
    reverse: dict[str, set[str]] = defaultdict(set)
    token_re = re.compile(r"[A-Za-z0-9_./-]+(?:\.py|\.json|\.ya?ml|\.sh|\.md|\.csv|\.cpp|\.hpp)")
    module_re = re.compile(r"(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)")
    for source in paths:
        file_path = root / source
        if not file_path.is_file() or file_path.stat().st_size > 2_000_000:
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for token in token_re.findall(text):
            normalized = token.lstrip("./")
            if normalized in known and normalized != source:
                reverse[normalized].add(source)
        for module in module_re.findall(text):
            target = module_paths.get(module)
            if target and target != source:
                reverse[target].add(source)
    return {path: sorted(readers) for path, readers in reverse.items()}


def _runtime_outputs(launcher: str) -> list[str]:
    return sorted(set(re.findall(r"\$RUN_ROOT/[A-Za-z0-9_./${}-]+", launcher)))


def _processes(launcher: str) -> list[tuple[str, str, int]]:
    lines = launcher.splitlines()
    processes: list[tuple[str, str, int]] = []
    previous = 0
    for index, line in enumerate(lines):
        if 'pids+=("$!")' not in line and 'v7_register_child "$!"' not in line:
            continue
        segment = "\n".join(lines[previous:index + 1])
        previous = index + 1
        scripts = re.findall(r"(?:python3\s+)?(scripts/[A-Za-z0-9_./-]+|\"\$[A-Z0-9_]+\")", segment)
        logs = re.findall(r"\$RUN_ROOT/([A-Za-z0-9_./${}-]+\.log)", segment)
        entrypoint = scripts[-1] if scripts else "launcher_subshell"
        log = logs[-1] if logs else f"spawn_{len(processes) + 1}.log"
        processes.append((entrypoint, log, index + 1))
    return processes


def _ref_classification(root: Path, ref: str) -> tuple[str, str, str]:
    if ref in {
        "refs/heads/main",
        "refs/heads/codex/v7-unified-system-legacy-eradication",
        "refs/remotes/origin/codex/v7-unified-system-legacy-eradication",
        "refs/remotes/origin/main", "refs/remotes/origin/HEAD",
    }:
        return "KEEP_CANONICAL", "active migration or canonical upstream ref", "classified"
    if ref == "refs/tags/v7-unification-pre-migration-8d9e8e60":
        return "KEEP_CANONICAL", "immutable rollback reference", "classified"
    if ref.startswith("refs/tags/"):
        return "ARCHIVE_HISTORY_ONLY", "historical exact-SHA cutover provenance", "classified"
    if ref.startswith("refs/heads/") or ref.startswith("refs/remotes/"):
        try:
            unique_count = int(_git(root, "rev-list", "--count", f"origin/main..{ref}").strip())
        except (subprocess.CalledProcessError, ValueError):
            unique_count = 0
        if unique_count:
            return (
                "MERGE_INTO_CANONICAL",
                f"{unique_count} commit(s) not reachable from origin/main require content audit",
                "unique_commit_audit_pending",
            )
        return (
            "DELETE_ACTIVE_LEGACY", "fully reachable from origin/main; redundant active ref",
            "deletion_deferred_until_phase_9",
        )
    return "ARCHIVE_HISTORY_ONLY", "tool-owned internal ref", "classified"


def build_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    paths = _paths(root)
    references = _static_references(root, paths)
    launcher_path = root / "scripts/paper_v7_execution_loop.sh"
    launcher = launcher_path.read_text(encoding="utf-8")
    entries: list[dict[str, Any]] = []
    for path in paths:
        classification = _classification_for_name(path)
        entries.append(_entry(
            surface_id=f"path:{path}", name=path, object_type="tracked_path",
            classification=classification, references=references.get(path),
        ))
    for ref, object_id in _refs(root):
        classification, unique, status = _ref_classification(root, ref)
        entry = _entry(
            surface_id=f"ref:{ref}", name=ref,
            object_type=("tag" if ref.startswith("refs/tags/") else "branch_or_remote_ref"),
            classification=classification, unique=unique, migration_status=status,
        )
        entry["snapshot_object_id"] = object_id
        entries.append(entry)
    for path in paths:
        if path.startswith("schemas/") or path.endswith(".schema.json"):
            entries.append(_entry(
                surface_id=f"schema:{path}", name=path, object_type="schema",
                classification=_classification_for_name(path), references=references.get(path),
                unique="versioned validation contract",
            ))
        if path.startswith(".github/workflows/"):
            entries.append(_entry(
                surface_id=f"workflow:{path}", name=path, object_type="workflow",
                classification=_classification_for_name(path), references=references.get(path),
                unique="CI, research, deployment, or cutover control surface",
            ))
    for output in _runtime_outputs(launcher):
        entries.append(_entry(
            surface_id=f"runtime_output:{output}", name=output,
            object_type="runtime_output", classification=_classification_for_name(output),
            references=["scripts/paper_v7_execution_loop.sh"],
            unique="launcher-declared runtime state, evidence, tape, status, or log surface",
        ))
    for number, (entrypoint, log, line) in enumerate(_processes(launcher), 1):
        identity = f"launcher_process:{number:02d}:{log}"
        entries.append(_entry(
            surface_id=identity, name=entrypoint, object_type="process",
            classification=_classification_for_name(f"{entrypoint}:{log}"),
            references=[f"scripts/paper_v7_execution_loop.sh:{line}"],
            unique=f"long-lived launcher child; log=$RUN_ROOT/{log}",
        ))
    for identity, name, classification, unique in (
        ("process:runtime_supervisor", "ops/v7_runtime_supervisor.py", "KEEP_CANONICAL", "single runtime lifecycle owner"),
        ("process:paper_launcher", "scripts/paper_v7_execution_loop.sh", "KEEP_CANONICAL", "canonical exact-SHA PAPER process launcher"),
        ("external:PAPER_host", "PAPER host runtime inspection", "EXTERNAL_BLOCKER", "SSH owner access required to compare running process and writer identities"),
        ("external:GitHub_admin", "GitHub required-check and ruleset administration", "EXTERNAL_BLOCKER", "authenticated repository administration required"),
    ):
        entries.append(_entry(
            surface_id=identity, name=name,
            object_type=("external_action" if identity.startswith("external:") else "process"),
            classification=classification, unique=unique,
        ))
    entries.sort(key=lambda row: row["surface_id"])
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "source_snapshot_sha": _git(root, "rev-parse", "HEAD").strip(),
        "coverage": {
            "tracked_or_intended_path_count": len(paths),
            "ref_count": len(_refs(root)),
            "schema_count": sum(row["object_type"] == "schema" for row in entries),
            "workflow_count": sum(row["object_type"] == "workflow" for row in entries),
            "runtime_output_count": sum(row["object_type"] == "runtime_output" for row in entries),
            "process_count": sum(row["object_type"] == "process" for row in entries),
            "external_blocker_count": sum(row["classification"] == "EXTERNAL_BLOCKER" for row in entries),
        },
        "entries": entries,
    }


def validate_manifest(value: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    if (
        value.get("schema") != SCHEMA
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
    ):
        raise ClassificationError("identity_or_safety_contract")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ClassificationError("entries_required")
    ids: set[str] = set()
    for row in entries:
        if not isinstance(row, dict) or not REQUIRED_FIELDS <= set(row):
            raise ClassificationError("entry_shape")
        identity = row.get("surface_id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ClassificationError("surface_identity_unique")
        ids.add(identity)
        classification = row.get("classification")
        if classification not in CLASSES:
            raise ClassificationError(f"classification:{identity}")
        authority = row.get("economic_authority")
        if not isinstance(authority, dict):
            raise ClassificationError(f"economic_authority:{identity}")
        if classification == "KEEP_ZERO_AUTHORITY_RESEARCH" and (
            authority.get("owner") is not None
            or authority.get("capabilities") != []
            or authority.get("executable") is not False
        ):
            raise ClassificationError(f"research_authority:{identity}")
        if classification == "KEEP_TEMPORARY_COMPATIBILITY" and not row.get("deletion_gate"):
            raise ClassificationError(f"temporary_without_deletion_gate:{identity}")
    if root is not None:
        expected_paths = set(_paths(root.resolve()))
        actual_paths = {
            row["path_or_ref"] for row in entries if row["object_type"] == "tracked_path"
        }
        if actual_paths != expected_paths:
            raise ClassificationError("tracked_path_coverage")
        expected_refs = {ref for ref, _ in _refs(root.resolve())}
        actual_refs = {
            row["path_or_ref"] for row in entries
            if row["object_type"] in {"tag", "branch_or_remote_ref"}
        }
        # A committed audit may include workstation-local or remote-tracking
        # refs that are intentionally absent from a clean CI checkout.  CI
        # must nevertheless fail if *its* visible ref namespace contains an
        # unclassified ref.  Extra immutable snapshot entries are evidence,
        # not a reproducibility defect.
        missing_refs = sorted(expected_refs - actual_refs)
        if missing_refs:
            raise ClassificationError(f"ref_coverage:{missing_refs}")
    counts = {name: 0 for name in sorted(CLASSES)}
    for row in entries:
        counts[row["classification"]] += 1
    return {
        "schema": "polymarket_v7_surface_classification_audit_v1",
        "passed": True,
        "entry_count": len(entries),
        "classification_counts": counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()
    try:
        if args.validate:
            value = json.loads(args.validate.read_text(encoding="utf-8"))
            result = validate_manifest(value, root=args.repository_root)
        else:
            value = build_manifest(args.repository_root)
            result = validate_manifest(value, root=args.repository_root)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8",
                )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, ClassificationError) as exc:
        print(f"v7_surface_classification: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
