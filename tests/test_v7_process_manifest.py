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


def test_manifest_matches_all_31_launcher_children_and_two_runtime_owners() -> None:
    report = resolve(ROOT, manifest())
    assert report["process_count"] == 22
    assert report["launcher_child_count"] == 20
    assert report["launcher_manifest_parity"] is True
    assert report["feed_zero_authority"] is True


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
    test_manifest_matches_all_31_launcher_children_and_two_runtime_owners()
    test_feed_process_cannot_gain_authority()
    test_launcher_child_cannot_escape_manifest_inventory()
