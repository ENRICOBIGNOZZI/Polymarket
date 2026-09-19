from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_research_causal_dataset import (
    BUNDLE_SCHEMA, SplitTimes, evaluate_bundle, process_jsonl, retention_admission,
    split_examples, strict_json,
)
from v7_research_economic_contract import ResearchContractError, canonical_hash
from test_v7_research_economic_contract import fixture, MS


def bundle():
    market, books, decision, latency, features = fixture()
    value = {"schema": BUNDLE_SCHEMA, "market": asdict(market), "decision": asdict(decision),
             "books": [asdict(f) for f in books.frames], "coverage": asdict(books.proof),
             "entry_latency": asdict(latency), "exit_latency": asdict(latency),
             "holding_ns": 200*MS, "features": features, "calendar_block": "block-1",
             "market_open_ns": 900*MS}
    return json.loads(json.dumps(value, default=str))


def example(shift_ms=0, market_id="m1", decision_id="d1", block="block-1"):
    row = evaluate_bundle(bundle())
    row["calendar_block"] = block
    row["market_open_ns"] += shift_ms * MS
    label = row["label"]
    label.update(market_id=market_id, decision_id=decision_id)
    for name in ("origin_ns", "feature_available_ns", "entry_ns", "exit_decision_ns",
                 "exit_ns", "information_end_ns", "label_available_ns"):
        label[name] += shift_ms * MS
    label["label_hash"] = canonical_hash({k: v for k, v in label.items() if k != "label_hash"})
    return row


def rehash(row):
    label = row["label"]
    label["label_hash"] = canonical_hash({k:v for k,v in label.items() if k != "label_hash"})
    return row


class CausalDatasetTests(unittest.TestCase):
    def times(self):
        return SplitTimes("host-boot-1", 500*MS, 3500*MS, 6500*MS, 9500*MS)

    def test_end_to_end_bundle(self):
        value = evaluate_bundle(bundle())
        self.assertEqual(value["label"]["status"], "COMPLETE")
        self.assertEqual(Decimal(value["label"]["net_pnl"]), Decimal("-0.07486"))
        self.assertEqual(value["features"]["optional_context"], None)
        self.assertEqual(value["bundle_hash"], canonical_hash(bundle()))

    def test_bundle_requires_recorded_feature_cut(self):
        value = bundle()
        value["features"]["external_return_bp"] = 999
        with self.assertRaisesRegex(ResearchContractError, "feature_cut_hash"):
            evaluate_bundle(value)

    def test_unknown_economics_are_excluded_not_filled_in(self):
        value = bundle()
        del value["market"]["taker_fee_rate"]
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"source.jsonl", Path(directory)/"out.jsonl"
            source.write_text(json.dumps(value)+"\n")
            result = process_jsonl(source, target)
            self.assertEqual(result["counts"], {"EXCLUDED": 1})
            row = json.loads(target.read_text())
            self.assertNotIn("net_pnl", row)
            self.assertIn("reason", row)

    def test_publication_manifest_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"source.jsonl", Path(directory)/"out.jsonl"
            source.write_text(json.dumps(bundle())+"\n")
            result = process_jsonl(source, target)
            self.assertEqual(result["output_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(result["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertFalse(result["automatic_promotion"])
            self.assertFalse(result["economic_performance_claim"])
            self.assertEqual(json.loads(target.with_name("out.jsonl.manifest.json").read_text()), result)
            before = target.read_bytes()
            with self.assertRaisesRegex(ResearchContractError, "immutable_output"):
                process_jsonl(source, target)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob(".v7-*")), [])

    def test_incomplete_tail_refuses_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"source.jsonl", Path(directory)/"out.jsonl"
            source.write_text(json.dumps(bundle()))
            with self.assertRaises(ResearchContractError):
                process_jsonl(source, target)
            self.assertFalse(target.exists())

    def test_duplicate_records_refuse_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"source.jsonl", Path(directory)/"out.jsonl"
            source.write_text((json.dumps(bundle())+"\n")*2)
            with self.assertRaisesRegex(ResearchContractError, "duplicate_bundle"):
                process_jsonl(source, target)
            self.assertFalse(target.exists())

    def test_input_and_output_size_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"source.jsonl", Path(directory)/"out.jsonl"
            source.write_text(json.dumps(bundle())+"\n")
            for kwargs in ({"maximum_input_bytes": 2}, {"maximum_output_bytes": 2}, {"maximum_line_bytes": 2}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ResearchContractError):
                    process_jsonl(source, target, **kwargs)
                self.assertFalse(target.exists())

    def test_source_symlink_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, link, target = root/"source.jsonl", root/"link.jsonl", root/"out.jsonl"
            source.write_text(json.dumps(bundle())+"\n")
            link.symlink_to(source)
            with self.assertRaises(ResearchContractError):
                process_jsonl(link, target)

    def test_strict_json(self):
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ResearchContractError):
                strict_json(raw)

    def test_cli_smoke(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"source.jsonl", Path(directory)/"out.jsonl"
            source.write_text(json.dumps(bundle())+"\n")
            run = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/"scripts/v7_research_causal_dataset.py"),
                                  "--input", str(source), "--output", str(target)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["counts"], {"COMPLETE": 1})

    def test_chronological_three_way_split(self):
        rows = [example(), example(3000, "m2", "d2", "block-2"), example(6000, "m3", "d3", "block-3")]
        result = split_examples(reversed(rows), self.times())
        self.assertEqual([len(result["partitions"][p]) for p in ("train", "validation", "audit")], [1,1,1])
        self.assertEqual(result["excluded"], {})
        self.assertEqual(result["unit"], "COUNTERFACTUAL_DECISION_NOT_TRADABLE_PORTFOLIO")

    def test_calendar_block_not_split_across_assets(self):
        rows = [example(), example(3000, "eth-market", "d2", "block-1")]
        result = split_examples(rows, self.times())
        self.assertTrue(all(not x for x in result["partitions"].values()))
        self.assertEqual(result["excluded"]["GROUP_CROSSES_TIME_BOUNDARY_OR_OUTSIDE_WINDOW"], 2)

    def test_market_not_split_across_blocks(self):
        result = split_examples([example(), example(3000, "m1", "d2", "block-2")], self.times())
        self.assertTrue(all(not x for x in result["partitions"].values()))

    def test_group_union_is_transitive(self):
        rows = [example(), example(0, "m2", "d2", "block-1"), example(3000, "m2", "d3", "block-2")]
        result = split_examples(rows, self.times())
        self.assertEqual(result["group_count"], 1)
        self.assertTrue(all(not x for x in result["partitions"].values()))

    def test_label_availability_and_embargo(self):
        row = example()
        row["label"]["label_available_ns"] = 3400*MS
        result = split_examples([rehash(row)], replace(self.times(), embargo_ns=100*MS))
        self.assertEqual(result["excluded"]["LABEL_UNAVAILABLE_BEFORE_NEXT_PARTITION"], 1)
        self.assertFalse(result["partitions"]["train"])

    def test_partial_positions_are_not_training_zeros(self):
        row = example()
        row["label"].update(status="OPEN_RESIDUAL", net_pnl=None)
        result = split_examples([rehash(row)], self.times())
        self.assertEqual(result["excluded"]["CENSORED_NOT_ZERO_IMPUTED"], 1)

    def test_future_feature_rejected(self):
        row = example()
        row["label"]["feature_available_ns"] = row["label"]["origin_ns"]+1
        with self.assertRaisesRegex(ResearchContractError, "future_feature"):
            split_examples([rehash(row)], self.times())

    def test_wrong_clock_domain_rejected(self):
        with self.assertRaisesRegex(ResearchContractError, "mixed_clock"):
            split_examples([example()], replace(self.times(), clock_domain="other-host"))

    def test_duplicate_decision_rejected(self):
        with self.assertRaisesRegex(ResearchContractError, "duplicate_decision"):
            split_examples([example(), example()], self.times())

    def test_label_or_feature_tampering_rejected(self):
        row = example()
        row["label"]["net_pnl"] = "999"
        with self.assertRaisesRegex(ResearchContractError, "label_hash"):
            split_examples([row], self.times())
        row = example()
        row["features"]["external_return_bp"] = 5
        with self.assertRaisesRegex(ResearchContractError, "feature_hash"):
            split_examples([row], self.times())

    def test_retention_is_bounded_and_never_authorizes_deletion(self):
        result = retention_admission(managed_bytes=59_999_999_990, new_bytes=10,
            budget_bytes=60_000_000_000, source_recoverable=True, labels_mature=True)
        self.assertTrue(result["admit_under_snapshot_budget"])
        self.assertFalse(result["source_deletion_authorized"])
        self.assertFalse(result["external_storage_allowed"])
        over = retention_admission(managed_bytes=60_000_000_000, new_bytes=1,
            budget_bytes=60_000_000_000, source_recoverable=False, labels_mature=False)
        self.assertFalse(over["admit_under_snapshot_budget"])
        self.assertFalse(over["prune_preconditions_met"])
        with self.assertRaises(ResearchContractError):
            retention_admission(managed_bytes=0, new_bytes=1, budget_bytes=60_000_000_001,
                                source_recoverable=True, labels_mature=True)


if __name__ == "__main__":
    unittest.main()
