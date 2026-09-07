from __future__ import annotations

import copy
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_process_manifest import ProcessManifestError, resolve  # noqa: E402


def manifest() -> dict:
    return json.loads((ROOT / "config/v7_process_manifest.json").read_text(encoding="utf-8"))


def test_manifest_matches_all_23_launcher_children_and_two_runtime_owners() -> None:
    report = resolve(ROOT, manifest())
    assert report["process_count"] == 25
    assert report["launcher_child_count"] == 23
    assert report["launcher_manifest_parity"] is True
    assert report["feed_zero_authority"] is True


def test_external_cancel_forward_runtime_consumes_hash_pinned_durable_baseline() -> None:
    value = manifest()
    row = next(item for item in value["processes"] if item["id"] == "external_cancel_forward_runtime")
    expected = {
        "${DURABLE_ROOT}/research/btc_m5_external_cancel_forward_report_v3.json",
        "${DURABLE_ROOT}/research/btc_m5_external_cancel_evidence_manifest_v1.json",
        "${DURABLE_ROOT}/research/btc_m5_external_cancel_episode_protocol_v3.json",
        "config/v7_crypto_execution_alpha.json",
    }
    assert expected <= set(row["inputs"])
    args = row["arguments"]
    assert args[args.index("--baseline-report") + 1] in expected
    assert args[args.index("--baseline-manifest") + 1] in expected
    assert args[args.index("--baseline-protocol") + 1] in expected
    launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert '--baseline-report "$EXTERNAL_CANCEL_BASELINE_REPORT"' in launcher
    assert '--baseline-manifest "$EXTERNAL_CANCEL_BASELINE_MANIFEST"' in launcher
    assert '--baseline-protocol "$EXTERNAL_CANCEL_BASELINE_PROTOCOL"' in launcher


def test_feed_process_cannot_gain_authority() -> None:
    value = manifest()
    value["profiles"]["feed"]["authority_flags"]["ledger"] = True
    try:
        resolve(ROOT, value)
    except ProcessManifestError as exc:
        assert str(exc).startswith("feed_authority_violation:")
    else:
        raise AssertionError("feed ledger authority accepted")


def test_launcher_child_cannot_escape_manifest_inventory() -> None:
    value = manifest()
    broken = copy.deepcopy(value)
    broken["processes"][0]["launcher_log"] = "unrecognized.log"
    broken["processes"][0]["outputs"].append("unrecognized.log")
    try:
        resolve(ROOT, broken)
    except ProcessManifestError as exc:
        assert str(exc) == "launcher_manifest_parity"
    else:
        raise AssertionError("launcher/manifest drift accepted")


if __name__ == "__main__":
    test_manifest_matches_all_23_launcher_children_and_two_runtime_owners()
    test_external_cancel_forward_runtime_consumes_hash_pinned_durable_baseline()
    test_feed_process_cannot_gain_authority()
    test_launcher_child_cannot_escape_manifest_inventory()
