from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHA = "d" * 40


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


provenance = load("v7_execution_provenance")


def record(stage: str, *, evidence_hash: str | None = None):
    payload_key = {
        "DECISION": "decision_hash", "SIGNED_ORDER": "order_payload_hash",
        "CLOB_ACCEPTED": "acceptance_payload_hash", "FILL": "fill_payload_hash",
        "SETTLEMENT": "settlement_payload_hash",
    }[stage]
    payload = {payload_key: {"DECISION": "d", "SIGNED_ORDER": "a", "CLOB_ACCEPTED": "c",
                             "FILL": "f", "SETTLEMENT": "e"}[stage] * 64}
    if stage == "SIGNED_ORDER":
        payload["signature_digest"] = "b" * 64
    return provenance.ProvenanceRecord(
        model_sha=SHA, lineage_id="lineage-1", stage=stage, event_ts_ms=1,
        payload=payload, evidence_record_hash=evidence_hash,
    )


class ExecutionProvenanceTests(unittest.TestCase):
    def complete_tape(self, root: Path) -> Path:
        path = provenance.provenance_path(root)
        with provenance.ProvenanceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            writer.append(record("DECISION"))
            writer.append(record("SIGNED_ORDER"))
            writer.append(record("CLOB_ACCEPTED", evidence_hash="a" * 64))
            writer.append(record("FILL", evidence_hash="b" * 64))
            writer.append(record("SETTLEMENT", evidence_hash="c" * 64))
        return path

    def test_complete_lineage_is_hash_chained_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.complete_tape(Path(directory))
            rows = list(provenance.iter_records(path))
            self.assertEqual([row.stage for row in rows], list(provenance.STAGES))
            self.assertEqual(rows[0].previous_stage_hash, "0" * 64)
            self.assertEqual(rows[-1].previous_stage_hash, rows[-2].record_hash)
            self.assertEqual(provenance.manifest(path, model_sha=SHA)["complete_lineages"], ["lineage-1"])

    def test_wrong_stage_order_and_evidence_shape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = provenance.provenance_path(Path(directory))
            with provenance.ProvenanceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
                with self.assertRaisesRegex(provenance.ProvenanceError, "lineage_must_start"):
                    writer.append(record("FILL", evidence_hash="b" * 64))
            with self.assertRaisesRegex(provenance.ProvenanceError, "stage_requirement"):
                record("FILL").validate(sealed=False)

    def test_multiple_partial_fills_are_chained_before_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = provenance.provenance_path(Path(directory))
            with provenance.ProvenanceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
                writer.append(record("DECISION"))
                writer.append(record("SIGNED_ORDER"))
                writer.append(record("CLOB_ACCEPTED", evidence_hash="a" * 64))
                first = writer.append(record("FILL", evidence_hash="b" * 64))
                second = writer.append(record("FILL", evidence_hash="c" * 64))
                terminal = writer.append(record("SETTLEMENT", evidence_hash="d" * 64))
            rows = list(provenance.iter_records(path))
            self.assertEqual([row.stage for row in rows], ["DECISION", "SIGNED_ORDER", "CLOB_ACCEPTED", "FILL", "FILL", "SETTLEMENT"])
            self.assertEqual(second.previous_stage_hash, first.record_hash)
            self.assertEqual(terminal.previous_stage_hash, second.record_hash)

    def test_tampered_stage_link_fails_independent_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.complete_tape(Path(directory))
            lines = path.read_text(encoding="utf-8").splitlines()
            value = json.loads(lines[2])
            value["previous_stage_hash"] = "0" * 64
            lines[2] = json.dumps(value, sort_keys=True, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(provenance.ProvenanceError, "lineage_stage_break|record_hash:mismatch"):
                list(provenance.iter_records(path))


if __name__ == "__main__":
    unittest.main()
