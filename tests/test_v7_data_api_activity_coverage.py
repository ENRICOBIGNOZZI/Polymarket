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
coverage = load("v7_data_api_activity_coverage")
SHA, WALLET = "1" * 40, "0x" + "12" * 20


def pages(specs: list[tuple[int, int, list[dict]]]):
    with tempfile.TemporaryDirectory() as directory:
        path = evidence.evidence_path(Path(directory))
        with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            return [writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="DATA_API_ACTIVITY", source_record_id=f"page-{offset}", received_ts_ms=1,
                request_method="GET", endpoint="https://data-api.polymarket.com/activity",
                query={"user": WALLET, "offset": str(offset), "limit": str(limit),
                       "excludeDepositsWithdrawals": "false"}, response=response,
            )) for offset, limit, response in specs]


class ActivityCoverageTests(unittest.TestCase):
    def test_contiguous_pages_with_terminal_short_page_are_complete(self) -> None:
        result = coverage.activity_coverage(pages([(0, 2, [{}, {}]), (2, 2, [{}])]), wallet=WALLET)
        self.assertEqual(result.activity_count, 3)
        self.assertEqual([page["offset"] for page in result.to_dict()["pages"]], [0, 2])

    def test_missing_terminal_or_deposit_exclusion_fails_closed(self) -> None:
        with self.assertRaisesRegex(coverage.ActivityCoverageError, "terminal_short"):
            coverage.activity_coverage(pages([(0, 1, [{}])]), wallet=WALLET)
        records = pages([(0, 2, [])])
        altered = records[0].__class__(**{**records[0].__dict__, "query": {**records[0].query, "excludeDepositsWithdrawals": "true"}, "record_hash": None})
        altered = altered.seal(records[0].previous_record_hash)
        with self.assertRaisesRegex(coverage.ActivityCoverageError, "scope"):
            coverage.activity_coverage([altered], wallet=WALLET)
