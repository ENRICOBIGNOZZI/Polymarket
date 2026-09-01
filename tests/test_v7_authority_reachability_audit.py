from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_authority_reachability_audit import (  # noqa: E402
    ReachabilityAuditError,
    audit,
)


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


class AuthorityReachabilityAuditTests(unittest.TestCase):
    def test_every_static_ledger_transport_edge_is_explained(self) -> None:
        result = audit(
            ROOT, load("config/v7_authority_registry.json"),
            load("config/v7_authority_edges.json"),
        )
        self.assertEqual(result["unexplained_edges"], [])
        self.assertTrue(result["audit_gate"]["complete_static_edge_coverage"])
        self.assertTrue(result["audit_gate"]["target_topology_complete"])
        self.assertEqual(result["known_migration_defect_count"], 0)

    def test_unregistered_writer_injection_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/unregistered.py").write_text("spool_event(event)\n")
            with self.assertRaisesRegex(ReachabilityAuditError, "unexplained_edges"):
                audit(
                    root, load("config/v7_authority_registry.json"),
                    load("config/v7_authority_edges.json"),
                )

    def test_duplicate_edge_registration_fails_closed(self) -> None:
        edges = load("config/v7_authority_edges.json")
        edges["edges"].append(copy.deepcopy(edges["edges"][0]))
        with self.assertRaisesRegex(ReachabilityAuditError, "edge_source_unique"):
            audit(ROOT, load("config/v7_authority_registry.json"), edges)


if __name__ == "__main__":
    unittest.main()
