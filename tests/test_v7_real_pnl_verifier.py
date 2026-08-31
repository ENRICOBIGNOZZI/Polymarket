from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ledger = load("v7_execution_ledger")
verifier = load("v7_real_pnl_verifier")
evidence = load("v7_real_pnl_evidence")
provenance = load("v7_execution_provenance")
wallet_snapshot = load("v7_wallet_balance_snapshot")
position_snapshot = load("v7_data_api_position_snapshot")
activity_coverage = load("v7_data_api_activity_coverage")
lifecycle = load("v7_polygon_lifecycle_journal")
SHA = "f" * 40
WALLET = "0x" + "12" * 20
OTHER = "0x" + "34" * 20
ZERO = "0x" + "0" * 40
CONDITION = "0x" + "ab" * 32
REDEEM_TX = "0x" + "de" * 32


def _word(value: int) -> str:
    return value.to_bytes(32, "big").hex()


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def _redeem_receipt() -> dict:
    return {
        "status": "0x1", "transactionHash": REDEEM_TX,
        "logs": [
            {"address": lifecycle.PUSD,
             "topics": [lifecycle.ERC20_TRANSFER, _topic(OTHER), _topic(WALLET)],
             "data": "0x" + _word(100_000_000)},
            {"address": lifecycle.CONDITIONAL_TOKENS,
             "topics": [lifecycle.ERC1155_TRANSFER_SINGLE, _topic(OTHER), _topic(WALLET), _topic(ZERO)],
             "data": "0x" + _word(123) + _word(100_000_000)},
        ],
    }


def entry(kind: str, source: str, source_id: str, postings: list[tuple[str, str, int]], evidence_hash: str,
          provenance_hash: str | None = None, metadata_extra: dict | None = None):
    metadata = {"evidence_record_hash": evidence_hash}
    if provenance_hash is not None:
        metadata["provenance_record_hash"] = provenance_hash
    if metadata_extra:
        metadata.update(metadata_extra)
    return ledger.EconomicJournalEntry(
        entry_type=kind,
        model_sha=SHA,
        observed_ts_ms=1_000,
        source=source,
        source_record_id=source_id,
        execution_mode="LIVE_OBSERVED",
        authenticated_execution=True,
        metadata=metadata,
        postings=tuple(ledger.JournalPosting(*posting) for posting in postings),
    )


class RealPnlVerifierTests(unittest.TestCase):
    def complete_journal(self, root: Path, *, trade_metadata_override: dict | None = None,
                         provenance_lineage_id: str = "order-1",
                         acceptance_event_type: str = "PLACEMENT",
                         trade_status: str = "TRADE_STATUS_CONFIRMED") -> tuple[Path, Path, Path]:
        evidence_path = evidence.evidence_path(root)
        with evidence.EvidenceTapeWriter(evidence_path, writer_id="test", model_sha=SHA) as writer:
            wallet = writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="WALLET_RPC", source_record_id="wallet-deposit-1",
                received_ts_ms=1, request_method="POST", endpoint="https://rpc.example.test",
                response={"result": "0x0"},
            ))
            writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="WALLET_RPC", source_record_id="wallet-cash-final",
                received_ts_ms=1, request_method="POST", endpoint="https://rpc.example.test",
                query={"chain_id": "137", "jsonrpc_method": "eth_call", "block_hash": "0x" + "ab" * 32,
                       "call_to": wallet_snapshot.PUSD,
                       "call_data": wallet_snapshot.erc20_balance_of_calldata("0x" + "12" * 20)},
                response={"jsonrpc": "2.0", "id": 1, "result": hex(159_999_995)},
            ))
            writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="WALLET_RPC", source_record_id="wallet-token-final",
                received_ts_ms=1, request_method="POST", endpoint="https://rpc.example.test",
                query={"chain_id": "137", "jsonrpc_method": "eth_call", "block_hash": "0x" + "ab" * 32,
                       "call_to": wallet_snapshot.CONDITIONAL_TOKENS,
                       "call_data": wallet_snapshot.erc1155_balance_of_calldata("0x" + "12" * 20, 123)},
                response={"jsonrpc": "2.0", "id": 2, "result": "0x0"},
            ))
            accepted = writer.append(evidence.clob_user_ws_record(SHA, 2, json.dumps({
                "topic": "user", "type": "order", "payload": {
                    "id": provenance_lineage_id, "owner": "api-key-1", "market": "condition-1",
                    "tokenId": "123", "side": "BUY", "originalSize": "100", "sizeMatched": "100",
                    "price": "0.40", "orderEventType": acceptance_event_type, "status": "MATCHED",
                    "timestamp": 1782753357256,
                },
            }, separators=(",", ":"))))
            clob = writer.append(evidence.clob_user_ws_record(SHA, 3, json.dumps({
                "topic": "user", "type": "trade", "payload": {
                    "id": "trade-1", "takerOrderId": "order-1", "owner": "api-key-1",
                    "market": "condition-1", "tokenId": "123", "side": "BUY", "size": "100",
                    "price": "0.40", "status": trade_status, "traderSide": "MAKER",
                    "feeRateBps": "0", "timestamp": 1782753357257,
                },
            }, separators=(",", ":"))))
            data = writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="DATA_API_ACTIVITY", source_record_id="data-redeem-1",
                received_ts_ms=3, request_method="GET", endpoint="https://data-api.polymarket.com/activity",
                query={"user": "0x" + "12" * 20, "offset": "0", "limit": "100",
                       "excludeDepositsWithdrawals": "false"},
                response=[{"type": "REDEEM", "transactionHash": "0x" + "ee" * 32, "timestamp": 3}],
            ))
            writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="DATA_API_POSITIONS", source_record_id="positions-final",
                received_ts_ms=3, request_method="GET", endpoint="https://data-api.polymarket.com/positions",
                query={"user": "0x" + "12" * 20}, response=[],
            ))
            polygon = writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="POLYGON_RPC", source_record_id="polygon-gas-1",
                received_ts_ms=4, request_method="POST", endpoint="https://polygon-rpc.example.test",
                response={"result": "0x0"},
            ))
            redeem = writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="POLYGON_RPC", source_record_id=REDEEM_TX,
                received_ts_ms=4, request_method="POST", endpoint="https://polygon-rpc.example.test",
                query={"chain_id": "137", "jsonrpc_method": "eth_getTransactionReceipt",
                       "transaction_hash": REDEEM_TX},
                response={"jsonrpc": "2.0", "id": 3, "result": _redeem_receipt()},
            ))
        provenance_path = provenance.provenance_path(root)
        payload_keys = {
            "DECISION": "decision_hash", "SIGNED_ORDER": "order_payload_hash",
            "CLOB_ACCEPTED": "acceptance_payload_hash", "FILL": "fill_payload_hash",
            "SETTLEMENT": "settlement_payload_hash",
        }
        evidence_hashes = {"CLOB_ACCEPTED": str(accepted.record_hash), "FILL": str(clob.record_hash),
                           "SETTLEMENT": str(redeem.record_hash)}
        hashes = {"DECISION": "d", "SIGNED_ORDER": "a", "CLOB_ACCEPTED": "c", "FILL": "f", "SETTLEMENT": "e"}
        rows = {}
        with provenance.ProvenanceTapeWriter(provenance_path, writer_id="test", model_sha=SHA) as writer:
            for stage in provenance.STAGES:
                payload = {payload_keys[stage]: hashes[stage] * 64}
                if stage == "SIGNED_ORDER":
                    payload["signature_digest"] = "b" * 64
                rows[stage] = writer.append(provenance.ProvenanceRecord(
                    model_sha=SHA, lineage_id=provenance_lineage_id, stage=stage, event_ts_ms=10,
                    payload=payload, evidence_record_hash=evidence_hashes.get(stage),
                ))
        path = ledger.canonical_ledger_path(root)
        with ledger.CanonicalLedgerWriter(path, writer_id="test", model_sha=SHA) as writer:
            writer.append_journal(entry("DEPOSIT", "WALLET_RPC", "wallet-deposit-1", [
                ("assets:cash:wallet", "pUSD", 100_000_000),
                ("equity:external_funding", "pUSD", -100_000_000),
            ], str(wallet.record_hash)))
            trade_metadata = {
                "clob_event_id": "trade-1", "clob_taker_order_id": "order-1",
                "condition_id": "condition-1", "token_id": "123", "side": "BUY",
                "trader_side": "MAKER", "fee_rate_bps": "0", "price": "0.40", "size": "100",
                "pUSD_decimals": 6, "token_decimals": 6,
            }
            if trade_metadata_override:
                trade_metadata.update(trade_metadata_override)
            writer.append_journal(entry("TRADE_FILL", "CLOB_USER_WS", clob.source_record_id, [
                ("assets:cash:wallet", "pUSD", -40_000_000),
                ("clearing:clob:cash", "pUSD", 40_000_000),
                ("assets:outcome:position", "token:123", 100_000_000),
                ("clearing:clob:token", "token:123", -100_000_000),
            ], str(clob.record_hash), str(rows["FILL"].record_hash), trade_metadata))
            writer.append_journal(lifecycle.lifecycle_journal(
                redeem, operation="REDEEM", wallet=WALLET, condition_id=CONDITION,
                provenance_record_hash=str(rows["SETTLEMENT"].record_hash),
            ))
            writer.append_journal(entry("WALLET_GAS", "POLYGON_RPC", "polygon-gas-1", [
                ("assets:cash:wallet", "pUSD", -5),
                ("clearing:polygon:gas", "pUSD", 5),
            ], str(polygon.record_hash)))
        return path, evidence_path, provenance_path

    def observed_snapshot(self, evidence_path: Path) -> dict:
        return wallet_snapshot.snapshot_from_evidence(
            evidence_path, model_sha=SHA, wallet="0x" + "12" * 20, token_ids=[123]
        ).to_dict()

    def observed_positions(self, evidence_path: Path) -> dict:
        record = next(item for item in evidence.iter_records(evidence_path)
                      if item.model_sha == SHA and item.source == "DATA_API_POSITIONS")
        return position_snapshot.position_snapshot(record, wallet="0x" + "12" * 20).to_dict()

    def observed_activity_coverage(self, evidence_path: Path) -> dict:
        records = [item for item in evidence.iter_records(evidence_path)
                   if item.model_sha == SHA and item.source == "DATA_API_ACTIVITY"]
        return activity_coverage.activity_coverage(records, wallet="0x" + "12" * 20).to_dict()

    def test_hash_chained_double_entry_journal_reconstructs_terminal_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(Path(directory))
            rows = ledger.load_journal_entries(path, expected_model_sha=SHA)
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0].previous_entry_hash, "0" * 64)
            self.assertEqual(rows[-1].previous_entry_hash, rows[-2].entry_hash)
            report = verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                     observed_positions=self.observed_positions(evidence_path),
                                     observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                     evidence_path=evidence_path, provenance_path=provenance_path)
            self.assertEqual(report["state"], "REAL_PNL_RECONCILED_UNSIGNED")
            self.assertEqual(report["reconstructed_realized_pnl_units"], 59_999_995)
            attestation = verifier.attest(report, operator_id="audit-key-1", signing_key="private-test-key")
            self.assertEqual(attestation["algorithm"], "HMAC-SHA256")
            self.assertEqual(len(attestation["signature"]), 64)
            missing_provenance = verifier.verify(path, model_sha=SHA, observed_balances={
                "assets:cash:wallet|pUSD": 155,
                "assets:outcome:position|token:123": 0,
            }, evidence_path=evidence_path)
            self.assertEqual(missing_provenance["state"], "MORE_EVIDENCE_REQUIRED")
            self.assertIn("provenance_tape_missing", missing_provenance["reason_codes"])

    def test_clob_fill_metadata_cannot_disagree_with_immutable_v2_trade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(
                Path(directory), trade_metadata_override={"price": "0.41"})
            report = verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                     observed_positions=self.observed_positions(evidence_path),
                                     observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                     evidence_path=evidence_path, provenance_path=provenance_path)
            self.assertEqual(report["state"], "MORE_EVIDENCE_REQUIRED")
            self.assertIn("journal_clob_fill_evidence_break", report["reason_codes"])

    def test_fill_cannot_borrow_a_complete_provenance_lineage_from_another_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(
                Path(directory), provenance_lineage_id="another-order")
            with self.assertRaisesRegex(verifier.VerificationError, "clob_fill_order_link"):
                verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                observed_positions=self.observed_positions(evidence_path),
                                observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                evidence_path=evidence_path, provenance_path=provenance_path)

    def test_clob_acceptance_must_be_a_placement_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(
                Path(directory), acceptance_event_type="UPDATE")
            with self.assertRaisesRegex(verifier.VerificationError, "clob_acceptance_event"):
                verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                observed_positions=self.observed_positions(evidence_path),
                                observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                evidence_path=evidence_path, provenance_path=provenance_path)

    def test_pre_final_clob_trade_cannot_support_a_complete_pnl_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(
                Path(directory), trade_status="TRADE_STATUS_MATCHED")
            with self.assertRaisesRegex(verifier.VerificationError, "clob_fill_event"):
                verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                observed_positions=self.observed_positions(evidence_path),
                                observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                evidence_path=evidence_path, provenance_path=provenance_path)

    def test_unbalanced_or_tampered_journal_fails_closed(self) -> None:
        with self.assertRaisesRegex(ledger.LedgerContractError, "unbalanced_postings"):
            entry("DEPOSIT", "WALLET_RPC", "bad", [
                ("assets:cash:wallet", "pUSD", 1),
                ("equity:external_funding", "pUSD", -2),
            ], "a" * 64).validate(sealed=False)
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(Path(directory))
            lines = path.read_text(encoding="utf-8").splitlines()
            corrupted = json.loads(lines[1])
            corrupted["postings"][0]["units"] = -41
            lines[1] = json.dumps(corrupted, sort_keys=True, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(verifier.VerificationError, "journal_hash_mismatch"):
                verifier.verify(path, model_sha=SHA, observed_balances={
                    "assets:cash:wallet|pUSD": 155, "assets:outcome:position|token:123": 0,
                }, evidence_path=evidence_path, provenance_path=provenance_path)

    def test_wallet_snapshot_is_rechecked_against_raw_rpc_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(Path(directory))
            observed = self.observed_snapshot(evidence_path)
            observed["balances"]["assets:cash:wallet|pUSD"] = 154
            with self.assertRaisesRegex(verifier.VerificationError, "wallet_snapshot_value_mismatch"):
                verifier.verify(path, model_sha=SHA, observed_balances=observed,
                                observed_positions=self.observed_positions(evidence_path),
                                observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                evidence_path=evidence_path, provenance_path=provenance_path)
            diagnostic = verifier.verify(path, model_sha=SHA, observed_balances={
                "assets:cash:wallet|pUSD": 155, "assets:outcome:position|token:123": 0,
            }, evidence_path=evidence_path, provenance_path=provenance_path)
            self.assertIn("wallet_snapshot_unverifiable", diagnostic["reason_codes"])

    def test_data_api_position_snapshot_is_rechecked_against_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(Path(directory))
            observed = self.observed_positions(evidence_path)
            observed["positions"] = {"token:123": 1}
            with self.assertRaisesRegex(verifier.VerificationError, "position_snapshot_value_mismatch"):
                verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                observed_positions=observed, evidence_path=evidence_path,
                                observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                provenance_path=provenance_path)

    def test_data_api_activity_coverage_is_rechecked_against_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(Path(directory))
            observed = self.observed_activity_coverage(evidence_path)
            observed["activity_count"] = 0
            with self.assertRaisesRegex(verifier.VerificationError, "activity_coverage_count_mismatch"):
                verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                observed_positions=self.observed_positions(evidence_path),
                                observed_activity_coverage=observed, evidence_path=evidence_path,
                                provenance_path=provenance_path)

    def test_terminal_provenance_must_link_to_its_settlement_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(Path(directory))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[2]["metadata"].pop("provenance_record_hash")
            rows[2].pop("entry_hash")
            rows[2]["entry_hash"] = verifier.digest(rows[2])
            rows[3]["previous_entry_hash"] = rows[2]["entry_hash"]
            rows[3].pop("entry_hash")
            rows[3]["entry_hash"] = verifier.digest(rows[3])
            path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
            report = verifier.verify(path, model_sha=SHA, observed_balances=self.observed_snapshot(evidence_path),
                                     observed_positions=self.observed_positions(evidence_path),
                                     observed_activity_coverage=self.observed_activity_coverage(evidence_path),
                                     evidence_path=evidence_path, provenance_path=provenance_path)
            self.assertIn("settlement_provenance_reference_break", report["reason_codes"])

    def test_independent_verifier_rejects_lookalike_clob_host(self) -> None:
        raw = {
            "record_kind": "REAL_PNL_EVIDENCE", "model_sha": SHA, "source": "CLOB_USER_ORDERS",
            "source_record_id": "order-page", "received_ts_ms": 1, "request_method": "GET",
            "endpoint": "https://clob.polymarket.com.attacker.test/orders", "response": [], "query": {},
            "authenticated_read": True, "record_id": "record-1", "schema_version": 1,
            "previous_record_hash": "0" * 64,
        }
        raw["record_hash"] = verifier.digest(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(verifier.VerificationError, "clob_endpoint"):
                verifier._evidence_hashes(path, expected_sha=SHA)

    def test_paper_or_open_inventory_never_claims_real_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = ledger.canonical_ledger_path(Path(directory))
            with ledger.CanonicalLedgerWriter(path, writer_id="test", model_sha=SHA) as writer:
                paper = ledger.EconomicJournalEntry(
                    entry_type="DEPOSIT", model_sha=SHA, observed_ts_ms=1,
                    source="WALLET_RPC", source_record_id="deposit",
                    postings=(
                        ledger.JournalPosting("assets:cash:wallet", "pUSD", 10),
                        ledger.JournalPosting("equity:external_funding", "pUSD", -10),
                    ),
                )
                writer.append_journal(paper)
            report = verifier.verify(path, model_sha=SHA, observed_balances={"assets:cash:wallet|pUSD": 10})
            self.assertEqual(report["state"], "MORE_EVIDENCE_REQUIRED")
            self.assertIn("not_all_entries_live_observed", report["reason_codes"])

    def test_journal_source_must_match_its_raw_evidence_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, evidence_path, provenance_path = self.complete_journal(root)
            clob_hash = evidence.manifest(evidence_path, model_sha=SHA)["record_hashes"][3]
            with ledger.CanonicalLedgerWriter(path, writer_id="test-2", model_sha=SHA) as writer:
                writer.append_journal(entry("WALLET_GAS", "WALLET_RPC", "bad-source-link", [
                    ("assets:cash:wallet", "pUSD", -1),
                    ("clearing:polygon:gas", "pUSD", 1),
                ], clob_hash))
            report = verifier.verify(path, model_sha=SHA, evidence_path=evidence_path, provenance_path=provenance_path,
                                     observed_balances={
                                         "assets:cash:wallet|pUSD": 154,
                                         "assets:outcome:position|token:123": 0,
                                     })
            self.assertIn("journal_evidence_reference_break", report["reason_codes"])

    def test_tampered_execution_provenance_is_rejected_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, evidence_path, provenance_path = self.complete_journal(Path(directory))
            lines = provenance_path.read_text(encoding="utf-8").splitlines()
            corrupted = json.loads(lines[3])
            corrupted["payload"]["fill_payload_hash"] = "0" * 64
            lines[3] = json.dumps(corrupted, sort_keys=True, separators=(",", ":"))
            provenance_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(verifier.VerificationError, "provenance_line_4:hash_mismatch"):
                verifier.verify(path, model_sha=SHA, observed_balances={
                    "assets:cash:wallet|pUSD": 155,
                    "assets:outcome:position|token:123": 0,
                }, evidence_path=evidence_path, provenance_path=provenance_path)


if __name__ == "__main__":
    unittest.main()
