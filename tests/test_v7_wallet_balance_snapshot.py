from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from dataclasses import replace
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
snapshot = load("v7_wallet_balance_snapshot")
SHA = "a" * 40
WALLET = "0x" + "12" * 20
BLOCK = "0x" + "ab" * 32


def sealed(*, to: str, data: str, result: int, source_id: str):
    with tempfile.TemporaryDirectory() as directory:
        path = evidence.evidence_path(Path(directory))
        with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            return writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="WALLET_RPC", source_record_id=source_id,
                received_ts_ms=1, request_method="POST", endpoint="https://rpc.example.test",
                query={"chain_id": "137", "jsonrpc_method": "eth_call", "block_hash": BLOCK,
                       "call_to": to, "call_data": data},
                response={"jsonrpc": "2.0", "id": 1, "result": hex(result)},
            ))


class WalletBalanceSnapshotTests(unittest.TestCase):
    def test_common_block_wallet_snapshot_produces_verifier_mapping(self) -> None:
        cash = sealed(to=snapshot.PUSD, data=snapshot.erc20_balance_of_calldata(WALLET),
                      result=2_500_000, source_id="cash")
        underlying = sealed(to=snapshot.USDCE, data=snapshot.erc20_balance_of_calldata(WALLET),
                            result=50, source_id="underlying")
        outcome = sealed(to=snapshot.CONDITIONAL_TOKENS,
                         data=snapshot.erc1155_balance_of_calldata(WALLET, 123),
                         result=1_000_000, source_id="outcome")
        result = snapshot.wallet_balance_snapshot(cash, [(123, outcome)], wallet=WALLET,
                                                  usdce_record=underlying)
        self.assertEqual(result.block_hash, BLOCK)
        self.assertEqual(result.verifier_balances(), {
            "assets:cash:wallet|pUSD": 2_500_000,
            "assets:cash:wallet|USDCe": 50,
            "assets:outcome:position|token:123": 1_000_000,
        })
        self.assertEqual(len(result.evidence_record_hashes), 3)
        self.assertEqual(result.to_dict()["balances"], result.verifier_balances())

    def test_wrong_calldata_mixed_block_and_duplicates_fail_closed(self) -> None:
        cash = sealed(to=snapshot.PUSD, data=snapshot.erc20_balance_of_calldata(WALLET), result=1, source_id="cash")
        wrong = sealed(to=snapshot.CONDITIONAL_TOKENS,
                       data=snapshot.erc1155_balance_of_calldata(WALLET, 2), result=0, source_id="wrong")
        with self.assertRaisesRegex(snapshot.WalletSnapshotError, "wrong_calldata"):
            snapshot.wallet_balance_snapshot(cash, [(1, wrong)], wallet=WALLET)
        valid = sealed(to=snapshot.CONDITIONAL_TOKENS,
                       data=snapshot.erc1155_balance_of_calldata(WALLET, 1), result=0, source_id="valid")
        with self.assertRaisesRegex(snapshot.WalletSnapshotError, "duplicate"):
            snapshot.wallet_balance_snapshot(cash, [(1, valid), (1, valid)], wallet=WALLET)

    def test_mixed_block_and_tape_selection_fail_closed(self) -> None:
        cash = sealed(to=snapshot.PUSD, data=snapshot.erc20_balance_of_calldata(WALLET), result=1, source_id="cash")
        outcome = sealed(to=snapshot.CONDITIONAL_TOKENS,
                         data=snapshot.erc1155_balance_of_calldata(WALLET, 1), result=0, source_id="outcome")
        altered = replace(outcome, query={**outcome.query, "block_hash": "0x" + "cd" * 32}, record_hash=None)
        altered = altered.seal(outcome.previous_record_hash)
        with self.assertRaisesRegex(snapshot.WalletSnapshotError, "mixed_block_hash"):
            snapshot.wallet_balance_snapshot(cash, [(1, altered)], wallet=WALLET)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = evidence.evidence_path(root)
            with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
                writer.append(cash)
                writer.append(outcome)
            result = snapshot.snapshot_from_evidence(path, model_sha=SHA, wallet=WALLET, token_ids=[1])
            self.assertEqual(result.pUSD_units, 1)


if __name__ == "__main__":
    unittest.main()
