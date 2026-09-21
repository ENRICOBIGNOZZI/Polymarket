from pathlib import Path
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"ops"))
import v7_maker_historical_research_ssm as m


def test_maker_historical_request_is_paper_only(tmp_path):
    request={
      "schema":"polymarket_v7_maker_historical_research_ssm_request_v1",
      "version":1,
      "request_id":"maker-historical-test-20260921",
      "instance_id":"i-0fba2bac9fdc5cbeb",
      "markout_horizon":"250ms",
      "minimum_clusters":20,
      "output_directory":"docs/research/maker-historical-2026-09-21",
      "taker_report_path":"docs/research/direct-action-value-2026-09-21/direct_action.json",
      "taker_report_blob_sha":"a"*40,
      "paper_only":True,
      "authenticated_execution":False,
      "real_order_submission":False,
      "real_capital_at_risk":False,
    }
    path=tmp_path/"request.json"
    path.write_text(json.dumps(request),encoding="utf-8")
    assert m.load_request(path)==request


def test_maker_historical_archive_sources_exist():
    for relative in m.SOURCE_PATHS:
        assert (ROOT/relative).is_file(),relative
    assert m.source_archive(ROOT)


def test_maker_historical_runner_has_no_order_authority():
    source=(ROOT/"ops/v7_maker_historical_research_ssm.py").read_text()
    for forbidden in (
        "submit_order","cancel_order","post_order",
        '"real_order_submission":True',
        '"authenticated_execution":True',
    ):
        assert forbidden not in source
    assert "automatic_promotion" in source
    assert "CANONICAL_LEDGER_HISTORICAL_MAKER_ROWS_ALL_MODEL_SHAS" in source
