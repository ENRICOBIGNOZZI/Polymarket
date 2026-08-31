from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evidence = load("v7_real_pnl_evidence")
positions = load("v7_data_api_position_snapshot")
SHA, WALLET = "e" * 40, "0x" + "12" * 20


def sealed(response):
    with tempfile.TemporaryDirectory() as directory:
        path = evidence.evidence_path(Path(directory))
        with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            return writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="DATA_API_POSITIONS", source_record_id="page-1", received_ts_ms=1,
                request_method="GET", endpoint="https://data-api.polymarket.com/positions",
                query={"user": WALLET}, response=response,
            ))


class DataApiPositionSnapshotTests(unittest.TestCase):
    def test_exact_positions_are_extracted_from_sealed_response(self) -> None:
        result = positions.position_snapshot(sealed([
            {"proxyWallet": WALLET, "asset": "123", "size": "1.25"},
        ]), wallet=WALLET)
        self.assertEqual(result.to_dict()["positions"], {"token:123": 1_250_000})

    def test_wrong_wallet_or_non_exact_size_fails_closed(self) -> None:
        with self.assertRaisesRegex(positions.PositionSnapshotError, "wrong_wallet"):
            positions.position_snapshot(sealed([{"proxyWallet": "0x" + "34" * 20, "asset": "1", "size": "1"}]), wallet=WALLET)
        with self.assertRaisesRegex(positions.PositionSnapshotError, "not_exact"):
            positions.position_snapshot(sealed([{"proxyWallet": WALLET, "asset": "1", "size": "0.0000001"}]), wallet=WALLET)
