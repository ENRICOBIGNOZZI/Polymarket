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
lifecycle = load("v7_polygon_lifecycle_journal")
SHA = "d" * 40
WALLET = "0x" + "12" * 20
OTHER = "0x" + "34" * 20
ZERO = "0x" + "0" * 40
CONDITION = "0x" + "ab" * 32
TX = "0x" + "cd" * 32


def word(value: int) -> str:
    return value.to_bytes(32, "big").hex()


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def erc20(from_address: str, to_address: str, amount: int) -> dict:
    return {"address": lifecycle.PUSD, "topics": [lifecycle.ERC20_TRANSFER, topic(from_address), topic(to_address)], "data": "0x" + word(amount)}


def batch(from_address: str, to_address: str, ids: list[int], values: list[int]) -> dict:
    ids_part = [len(ids), *ids]
    values_offset = 32 * (2 + len(ids_part))
    words = [64, values_offset, *ids_part, len(values), *values]
    return {"address": lifecycle.CONDITIONAL_TOKENS,
            "topics": [lifecycle.ERC1155_TRANSFER_BATCH, topic(OTHER), topic(from_address), topic(to_address)],
            "data": "0x" + "".join(word(value) for value in words)}


def sealed_receipt(logs: list[dict], *, query: dict | None = None):
    receipt = {"status": "0x1", "transactionHash": TX, "logs": logs}
    with tempfile.TemporaryDirectory() as directory:
        path = evidence.evidence_path(Path(directory))
        with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            return writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="POLYGON_RPC", source_record_id=TX,
                received_ts_ms=1_000, request_method="POST", endpoint="https://polygon-rpc.example.test",
                query=query if query is not None else {
                    "chain_id": "137", "jsonrpc_method": "eth_getTransactionReceipt", "transaction_hash": TX,
                },
                response={"jsonrpc": "2.0", "result": receipt},
            ))


class PolygonLifecycleJournalTests(unittest.TestCase):
    def test_split_and_merge_decode_erc1155_batch_and_pusd_base_units(self) -> None:
        split = lifecycle.lifecycle_journal(sealed_receipt([
            erc20(WALLET, OTHER, 1_000_000), batch(ZERO, WALLET, [11, 22], [1_000_000, 1_000_000]),
        ]), operation="SPLIT", wallet=WALLET, condition_id=CONDITION)
        split.validate(sealed=False)
        self.assertEqual(split.entry_type, "TOKEN_SPLIT")
        self.assertEqual(split.postings[0].units, -1_000_000)
        merge = lifecycle.lifecycle_journal(sealed_receipt([
            erc20(OTHER, WALLET, 1_000_000), batch(WALLET, ZERO, [11, 22], [1_000_000, 1_000_000]),
        ]), operation="MERGE", wallet=WALLET, condition_id=CONDITION)
        self.assertEqual(merge.entry_type, "TOKEN_MERGE")
        self.assertEqual(merge.postings[0].units, 1_000_000)

    def test_redeem_allows_losing_inventory_but_requires_positive_payout(self) -> None:
        entry = lifecycle.lifecycle_journal(sealed_receipt([
            erc20(OTHER, WALLET, 1_000_000), batch(WALLET, ZERO, [11, 22], [1_000_000, 1_000_000]),
        ]), operation="REDEEM", wallet=WALLET, condition_id=CONDITION,
        provenance_record_hash="a" * 64)
        entry.validate(sealed=False)
        self.assertEqual(entry.entry_type, "TOKEN_REDEEM")
        self.assertEqual(entry.metadata["provenance_record_hash"], "a" * 64)
        self.assertEqual(entry.metadata["wallet"], WALLET.lower())

    def test_invalid_operation_or_unbalanced_receipt_fails_closed(self) -> None:
        record = sealed_receipt([erc20(WALLET, OTHER, 1_000_000), batch(ZERO, WALLET, [11, 22], [1, 2])])
        with self.assertRaisesRegex(lifecycle.PolygonLifecycleError, "split:invariant"):
            lifecycle.lifecycle_journal(record, operation="SPLIT", wallet=WALLET, condition_id=CONDITION)
        with self.assertRaisesRegex(lifecycle.PolygonLifecycleError, "unsupported"):
            lifecycle.lifecycle_journal(record, operation="WRAP", wallet=WALLET, condition_id=CONDITION)

    def test_receipt_must_be_the_exact_polygon_receipt_query(self) -> None:
        record = sealed_receipt([
            erc20(OTHER, WALLET, 1_000_000), batch(WALLET, ZERO, [11, 22], [1_000_000, 1_000_000]),
        ], query={})
        with self.assertRaisesRegex(lifecycle.PolygonLifecycleError, "receipt:query"):
            lifecycle.lifecycle_journal(
                record, operation="REDEEM", wallet=WALLET, condition_id=CONDITION,
            )


if __name__ == "__main__":
    unittest.main()
