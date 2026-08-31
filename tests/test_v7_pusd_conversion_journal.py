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
conversion = load("v7_pusd_conversion_journal")
SHA, WALLET, OTHER = "c" * 40, "0x" + "12" * 20, "0x" + "34" * 20
TX, ZERO = "0x" + "ab" * 32, "0x" + "0" * 40


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def transfer(asset: str, sender: str, recipient: str, amount: int) -> dict:
    return {"address": asset, "topics": [conversion.ERC20_TRANSFER, topic(sender), topic(recipient)],
            "data": "0x" + amount.to_bytes(32, "big").hex()}


def receipt(operation: str, logs: list[dict]):
    with tempfile.TemporaryDirectory() as directory:
        path = evidence.evidence_path(Path(directory))
        with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            return writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="POLYGON_RPC", source_record_id=TX, received_ts_ms=1,
                request_method="POST", endpoint="https://polygon-rpc.example.test",
                query={"chain_id": "137", "jsonrpc_method": "eth_getTransactionReceipt",
                       "transaction_hash": TX},
                response={"result": {"status": "0x1", "transactionHash": TX,
                                     "to": conversion.ONRAMP if operation == "WRAP" else conversion.OFFRAMP,
                                     "logs": logs}},
            ))


class PusdConversionJournalTests(unittest.TestCase):
    def test_wrap_and_unwrap_are_balanced_and_exact(self) -> None:
        wrap = conversion.conversion_journal(receipt("WRAP", [
            transfer(conversion.USDCE, WALLET, OTHER, 1_000_000),
            transfer(conversion.PUSD, ZERO, WALLET, 1_000_000),
        ]), operation="WRAP", wallet=WALLET)
        wrap.validate(sealed=False)
        self.assertEqual(wrap.entry_type, "PUSD_WRAP")
        self.assertEqual(wrap.postings[0].units, 1_000_000)
        unwrap = conversion.conversion_journal(receipt("UNWRAP", [
            transfer(conversion.PUSD, WALLET, ZERO, 1_000_000),
            transfer(conversion.USDCE, OTHER, WALLET, 1_000_000),
        ]), operation="UNWRAP", wallet=WALLET)
        self.assertEqual(unwrap.entry_type, "PUSD_UNWRAP")
        self.assertEqual(unwrap.postings[0].units, -1_000_000)

    def test_wrong_ramp_or_non_one_to_one_receipt_fails_closed(self) -> None:
        record = receipt("WRAP", [
            transfer(conversion.USDCE, WALLET, OTHER, 1_000_000),
            transfer(conversion.PUSD, ZERO, WALLET, 999_999),
        ])
        with self.assertRaisesRegex(conversion.PusdConversionError, "invariant"):
            conversion.conversion_journal(record, operation="WRAP", wallet=WALLET)


if __name__ == "__main__":
    unittest.main()
