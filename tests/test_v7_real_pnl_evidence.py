from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v7_real_pnl_evidence_test", ROOT / "scripts" / "v7_real_pnl_evidence.py")
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evidence
spec.loader.exec_module(evidence)
SHA = "9" * 40


def trade_wire(trade_id: str = "trade-1") -> str:
    return json.dumps({
        "topic": "user", "type": "trade", "payload": {
            "id": trade_id, "takerOrderId": "order-1", "owner": "api-key-1",
            "market": "condition-1", "tokenId": "token-1", "side": "BUY",
            "size": "10", "price": "0.52", "status": "TRADE_STATUS_MATCHED",
            "traderSide": "MAKER", "feeRateBps": "0", "timestamp": 1782753357257,
        },
    }, separators=(",", ":"))


class RealPnlEvidenceTests(unittest.TestCase):
    def test_v2_user_order_frame_is_normalized_only_after_raw_validation(self) -> None:
        wire = json.dumps({
            "topic": "user", "type": "order", "payload": {
                "id": "order-1", "owner": "api-key-1", "market": "condition-1", "tokenId": "token-1",
                "side": "BUY", "originalSize": "10", "sizeMatched": "0", "price": "0.52",
                "orderEventType": "PLACEMENT", "status": "LIVE", "timestamp": 1782753357257,
            },
        }, separators=(",", ":"))
        event = evidence.parse_clob_user_ws_wire(wire)
        self.assertEqual(event["event_type"], "order")
        self.assertEqual(event["asset_id"], "token-1")
        self.assertEqual(event["order_event_type"], "PLACEMENT")
        with self.assertRaisesRegex(evidence.EvidenceError, "frame_shape"):
            evidence.parse_clob_user_ws_wire(json.dumps({"event_type": "trade"}))

    def test_read_only_evidence_is_hash_chained_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = evidence.evidence_path(Path(directory))
            with evidence.EvidenceTapeWriter(path, writer_id="collector", model_sha=SHA) as writer:
                first = writer.append(evidence.EvidenceRecord(
                    model_sha=SHA, source="DATA_API_ACTIVITY", source_record_id="activity-1",
                    received_ts_ms=1, request_method="GET",
                    endpoint="https://data-api.polymarket.com/activity", response=[],
                ))
                second = writer.append(evidence.clob_user_ws_record(SHA, 2, trade_wire("fill-1")))
            self.assertEqual(second.previous_record_hash, first.record_hash)
            report = evidence.manifest(path, model_sha=SHA)
            self.assertEqual(report["records"], 2)
            self.assertEqual(report["head_hash"], second.record_hash)

    def test_non_read_only_or_tampered_evidence_fails_closed(self) -> None:
        with self.assertRaisesRegex(evidence.EvidenceError, "not_read_only"):
            evidence.EvidenceRecord(
                model_sha=SHA, source="DATA_API_ACTIVITY", source_record_id="activity-1",
                received_ts_ms=1, request_method="POST",
                endpoint="https://data-api.polymarket.com/activity", response=[],
            ).validate(sealed=False)
        with tempfile.TemporaryDirectory() as directory:
            path = evidence.evidence_path(Path(directory))
            with evidence.EvidenceTapeWriter(path, writer_id="collector", model_sha=SHA) as writer:
                writer.append(evidence.EvidenceRecord(
                    model_sha=SHA, source="DATA_API_POSITIONS", source_record_id="position-1",
                    received_ts_ms=1, request_method="GET",
                    endpoint="https://data-api.polymarket.com/positions", response=[],
                ))
            row = json.loads(path.read_text(encoding="utf-8"))
            row["response"] = {"tampered": True}
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(evidence.EvidenceError, "record_hash:mismatch"):
                list(evidence.iter_records(path))
        with self.assertRaisesRegex(evidence.EvidenceError, "not_polymarket"):
            evidence.EvidenceRecord(
                model_sha=SHA, source="CLOB_USER_ORDERS", source_record_id="order-1",
                received_ts_ms=1, request_method="GET", authenticated_read=True,
                endpoint="https://clob.polymarket.com.attacker.test/orders", response=[],
            ).validate(sealed=False)

    def test_authenticated_user_ws_wire_is_preserved_and_hash_linked(self) -> None:
        wire = trade_wire()
        record = evidence.clob_user_ws_record(SHA, 2, wire)
        self.assertTrue(record.authenticated_read)
        self.assertEqual(record.endpoint, evidence.USER_WS_ENDPOINT)
        self.assertEqual(record.response["wire_json"], wire)
        self.assertEqual(record.response["wire_sha256"], __import__("hashlib").sha256(wire.encode()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            path = evidence.evidence_path(Path(directory))
            with evidence.EvidenceTapeWriter(path, writer_id="user-ws", model_sha=SHA) as writer:
                sealed = writer.append(record)
            self.assertEqual(evidence.manifest(path, model_sha=SHA)["sources"], ["CLOB_USER_WS"])
            raw = sealed.to_dict()
            raw["response"]["event"]["price"] = "0.99"
            with self.assertRaisesRegex(evidence.EvidenceError, "wire_or_event_mismatch"):
                evidence.EvidenceRecord.from_dict(raw)

    def test_user_ws_rejects_non_economic_frames_and_duplicate_json_keys(self) -> None:
        with self.assertRaisesRegex(evidence.EvidenceError, "frame_shape"):
            evidence.parse_clob_user_ws_wire('{"type":"PONG"}')
        duplicate = ('{"topic":"user","topic":"user","type":"trade","payload":{"id":"t",'
                     '"takerOrderId":"o","owner":"k","market":"m","tokenId":"a","side":"BUY",'
                     '"size":"1","price":"0.5","status":"TRADE_STATUS_MATCHED","timestamp":1}}')
        with self.assertRaisesRegex(evidence.EvidenceError, "wire_not_json_object"):
            evidence.parse_clob_user_ws_wire(duplicate)


if __name__ == "__main__":
    unittest.main()
