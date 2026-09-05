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
    # Explicit sub-second research evidence only. These surfaces have no OMS,
    # capital, inventory, signer or production-process authority.
    "crypto_book_observer", "crypto_book_tape",
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


def equivalent_ref_surface_ids(surface_id: str) -> tuple[str, ...]:
    """Return portable local/remote-tracking identities for one branch ref.

    A full clone exposes upstream branches under ``refs/remotes/origin`` while
    the workstation on which the audit snapshot was produced may expose the
    same branch under ``refs/heads``.  These namespaces describe the same
    branch surface for classification purposes.  Tags and ``origin/HEAD`` are
    intentionally never aliased.
    """
    prefix = "ref:"
    if not surface_id.startswith(prefix):
        return (surface_id,)
    ref = surface_id[len(prefix):]
    remote_prefix = "refs/remotes/origin/"
    local_prefix = "refs/heads/"
    if ref.startswith(remote_prefix) and ref != "refs/remotes/origin/HEAD":
        branch = ref[len(remote_prefix):]
        return (surface_id, f"{prefix}{local_prefix}{branch}")
    if ref.startswith(local_prefix):
        branch = ref[len(local_prefix):]
        return (surface_id, f"{prefix}{remote_prefix}{branch}")
    return (surface_id,)


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