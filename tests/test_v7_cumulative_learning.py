"""Synthetic regression evidence only; never a profitability fixture."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import platform
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.learning.common import canonical, digest, research_host
from research.learning.catalog import inventory, NATIVE, LABEL
from research.learning.dataset import native_example, materialize, validate_label
from research.learning.daily import midnight
from research.learning.registry import transition, promotion_gates, GATES


def native(**extra):
    return {"schema": NATIVE, "server_id": "test", "run_id": "run", "capture_id": "capture",
            "market_id": "market", "token_id": "up", "model_sha": "a"*40, "kind": 2,
            "paper_only": True, "execution_authority": False, "asset": "BTC", "horizon": "M5",
            "decision_monotonic_ns": 200_000_000_000, "decision_wall_ns": 1000_000_000_000,
            "receive_monotonic_ns": 199_990_000_000, "trigger_monotonic_ns": 199_995_000_000,
            "evaluated_grid_monotonic_ns": 199_999_000_000,
            "close_monotonic_ns": 310_000_000_000, "close_wall_ns": 1110_000_000_000,
            "valid_until_monotonic_ns": 200_020_000_000, "signal_version": 5, "sequence": 1,
            "direction": 1, "signal_age_ns": 5_000_000, "tte_ns": 110_000_000_000,
            "bid_e4": 4900, "ask_e4": 5100, "tick_e4": 100, "bid_quantity": 2_000_000,
            "ask_quantity": 3_000_000, "book_valid": True, "accepted": False,
            "reason": "PROBABILITY_UNAVAILABLE", "binance_return_100ms_bp": 2.,
            "confirmation_venue": "COINBASE", "confirmation_return_100ms_bp": 1., **extra}


def label(**extra):
    raw = {"id": "market", "closed": True, "umaResolutionStatus": "resolved",
           "clobTokenIds": '["up","down"]', "outcomePrices": '["1","0"]'}
    return {"schema": LABEL, "provider": "POLYMARKET_GAMMA_PUBLIC", "market_id": "market",
            "closed": True, "resolution_status": "resolved", "information_ns": 1200_000_000_000,
            "payouts": {"up": 1., "down": 0.}, "public_response": raw,
            "public_response_sha256": digest(canonical(raw)), **extra}


def write(path, rows):
    path.write_bytes(b"".join(canonical(r)+b"\n" for r in rows))


def test_rejected_decisions_remain_population_and_missing_not_zero():
    r = native_example(native())
    assert r["accepted"] is False and r["reason"] == "PROBABILITY_UNAVAILABLE"
    assert r["outcome"] is None and r["features"]["normalized_shock"] is None
    assert r["features"]["pm_probability"] == .5
    assert r["features"]["spread_ticks"] == 2
    assert r["feature_information_ns"] <= r["decision_ns"]


@pytest.mark.parametrize("changes", [
    {"receive_monotonic_ns": 200_000_000_001}, {"trigger_monotonic_ns": 200_000_000_001},
    {"external_features": {"input_receive_ns": 201_000_000_000}},
    {"decision_wall_ns": 0}, {"direction": 0}, {"signal_version": 0},
    {"probability_forecast": .6, "probability_input_token_id": "down"},
])
def test_temporal_leakage_and_malformed_signal_fail_closed(changes):
    with pytest.raises(ValueError):
        native_example(native(**changes))


def test_settlement_provenance_is_not_a_self_attestation():
    validate_label(label())
    for change in ({"payouts": {"up": 0., "down": 1.}}, {"resolution_status": "pending"},
                   {"public_response_sha256": "0"*64}, {"payouts": {"up": .5, "down": .5}}):
        with pytest.raises(ValueError):
            validate_label(label(**change))


def test_dataset_regeneration_dedup_and_archive_readability(tmp_path):
    source = tmp_path/"source"; source.mkdir(); out = tmp_path/"private"
    write(source/"native.jsonl", [native(), native(sequence=2), label()])
    (source/"copy.jsonl.gz").write_bytes(gzip.compress((source/"native.jsonl").read_bytes(), mtime=0))
    c = inventory([source], out)
    assert c["files"] == 2 and len(c["source_revisions"]) == 2
    args = (out/"store", c["source_revisions"], out, 1300_000_000_000, "a"*40)
    a, rows = materialize(*args)
    assert a["decision_rows"] == 2 and a["signal_rows"] == 1
    assert rows[0]["outcome"] == 1 and rows[0]["training_eligible"]
    (source/"native.jsonl").unlink(); (source/"copy.jsonl.gz").unlink()
    b, restored = materialize(*args)
    assert a == b and rows == restored


def test_pending_and_labels_after_cutoff_are_not_training_zeros(tmp_path):
    source = tmp_path/"source"; source.mkdir(); out = tmp_path/"private"
    write(source/"native.jsonl", [native(), label()])
    c = inventory([source], out)
    m, rows = materialize(out/"store", c["source_revisions"], out, 1150_000_000_000, "a"*40)
    assert rows[0]["outcome"] is None and not rows[0]["training_eligible"]
    assert m["unresolved"] == 1 and m["labeled_markets"] == 0


def test_label_cannot_predate_decision(tmp_path):
    source = tmp_path/"source"; source.mkdir(); out = tmp_path/"private"
    write(source/"native.jsonl", [native(), label(information_ns=900_000_000_000)])
    c = inventory([source], out)
    with pytest.raises(ValueError, match="LEAKAGE"):
        materialize(out/"store", c["source_revisions"], out, 1300_000_000_000, "a"*40)


def test_cumulative_source_union_and_new_labels(tmp_path):
    source = tmp_path/"source"; source.mkdir(); out = tmp_path/"private"
    write(source/"native.jsonl", [native()]); c1 = inventory([source], out)
    write(source/"labels.jsonl", [label()]); c2 = inventory([source], out)
    _, before = materialize(out/"store", c1["source_revisions"], out, 1300_000_000_000, "a"*40)
    _, after = materialize(out/"store", c1["source_revisions"]+c2["source_revisions"], out, 1300_000_000_000, "a"*40)
    assert before[0]["outcome"] is None and after[0]["outcome"] == 1


def test_zurich_midnight_dst_and_safe_catchup():
    _, summer = midnight(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    _, winter = midnight(datetime(2026, 1, 2, 8, tzinfo=timezone.utc))
    assert datetime.fromtimestamp(summer/1e9, timezone.utc).hour == 22
    assert datetime.fromtimestamp(winter/1e9, timezone.utc).hour == 23
    date, _ = midnight(datetime(2026, 7, 1, 22, 1, tzinfo=timezone.utc))
    assert date == "2026-07-02"


def test_registry_no_automatic_promotion_or_small_cohort(tmp_path):
    sha = "a"*64; gates = {k: True for k in GATES}
    transition(tmp_path, sha, "TRAINED")
    with pytest.raises(ValueError):
        transition(tmp_path, sha, "PAPER_ELIGIBLE", evidence=gates)
    with pytest.raises(ValueError, match="GATES"):
        transition(tmp_path, sha, "VALIDATED_OFFLINE", evidence={})
    transition(tmp_path, sha, "VALIDATED_OFFLINE", evidence=gates)
    transition(tmp_path, sha, "PAPER_ELIGIBLE", evidence=gates)
    with pytest.raises(ValueError, match="NO_AUTO"):
        transition(tmp_path, sha, "PAPER_FORWARD_TEST", evidence=gates)
    activate = {"explicit_activation": True, "artifact_sha256": sha, "duration_seconds": 7200}
    transition(tmp_path, sha, "PAPER_FORWARD_TEST", evidence=gates, activation=activate)
    with pytest.raises(ValueError, match="TWO_HOUR"):
        transition(tmp_path, sha, "PROMOTED", evidence=gates, activation=activate)
    assert not promotion_gates({k: "true" for k in GATES})["passed"]


def test_unenrolled_or_wrong_research_host_cannot_train(tmp_path):
    (tmp_path/"research_host.json").write_text(json.dumps({"schema": "v7_research_host_v1",
        "hostname": "london", "role": "RESEARCH_ONLY"}))
    with pytest.raises(ValueError, match="LONDON_CANNOT_TRAIN"):
        research_host(tmp_path)


def synthetic_rows(n=300):
    import math
    rows = []
    for i in range(n):
        t = (i+1)*86_400_000_000_000
        rows.append({"decision_id": str(i), "signal_id": str(i), "market_id": str(i),
            "decision_ns": t, "feature_information_ns": t-1, "information_end_ns": t+100,
            "label_information_ns": t+101, "training_eligible": True, "outcome": i % 2,
            "asset": "BTC", "horizon": "M5", "stratum": "synthetic", "pm_probability": .45+.1*(i % 2),
            "features": {"signal": math.sin(i), "depth": None if i % 5 == 0 else float(i % 7)},
            "execution": None})
    return rows


def test_market_weighting_duplicates_and_purge():
    pytest.importorskip("numpy")
    from research.learning.validation import partition, weights
    rows = synthetic_rows(20); dup = {**rows[0], "decision_id": "duplicate"}
    w = weights(rows+[dup]); assert w[0] == .5 and w[-1] == .5
    parts, _ = partition(rows, [0, rows[10]["decision_ns"], rows[-1]["decision_ns"]+200], embargo_ns=0)
    assert not {r["market_id"] for r in parts[0]} & {r["market_id"] for r in parts[1]}
    # An otherwise invalid repeated market on the other side still groups out.
    cross = {**rows[-1], "market_id": rows[0]["market_id"], "decision_id": "cross", "training_eligible": False}
    parts, excluded = partition(rows+[cross], [0, rows[10]["decision_ns"], rows[-1]["decision_ns"]+200], embargo_ns=0)
    assert all(r["market_id"] != "0" for p in parts for r in p)
    assert excluded["MARKET_CROSSES_BOUNDARY"] == 2


def test_deterministic_models_pm_baseline_and_calibration_separation():
    pytest.importorskip("sklearn")
    import numpy as np
    from research.learning.models import Candidate, FAMILIES, calibrate, calibrated
    rows = synthetic_rows(); train, cal, valid = rows[:150], rows[150:220], rows[220:]
    for family in FAMILIES:
        a = Candidate(family).fit(train, rows[150]["decision_ns"])
        b = Candidate(family).fit(train, rows[150]["decision_ns"])
        assert a.parameters() == b.parameters()
        assert np.array_equal(a.predict(valid), b.predict(valid))
        if family == "pm":
            assert np.allclose(a.predict(valid), [r["pm_probability"] for r in valid])
        params = calibrate(a.predict(cal), cal, "platt")
        assert np.isfinite(calibrated(a.predict(valid), params)).all()
        with pytest.raises(ValueError, match="AVAILABILITY"):
            a.predict(train)


def test_execution_censoring_partial_fill_costs():
    pytest.importorskip("sklearn")
    from research.learning.execution import execution_label
    row = {**native_example(native()), "outcome": 1, "label_information_ns": 1200_000_000_000}
    assert execution_label(row, None)["net_pnl"] is None
    arrival = {"coverage_verified": True, "arrival_ns": row["decision_ns"]+1,
        "information_ns": row["decision_ns"]+2, "decision_id": row["decision_id"],
        "semantics": "ARRIVAL_TOP_FAK_ACTUAL_PRICE_PARTIAL_FILL_V2", "source_revisions": ["a"*64],
        "intended_quantity": 5., "filled_quantity": 2., "execution_price": .5,
        "fees": .02, "execution_cost": .01, "slippage": .1, "action": "FAK_5_SHARES"}
    result = execution_label(row, arrival)
    assert result["state"] == "PARTIAL_FILL" and result["net_pnl"] == pytest.approx(.97)
    assert execution_label({**row, "outcome": None}, arrival)["net_pnl"] is None


def test_snapshot_labels_and_tar_members_are_repeatably_readable(tmp_path):
    import tarfile
    source = tmp_path/'source'; source.mkdir(); out = tmp_path/'private'
    snapshot = source/'settlement.json'
    snapshot.write_text(json.dumps(label(), indent=2))  # No terminal newline.
    write(source/'native.jsonl', [native()])
    with tarfile.open(source/'history.tar.gz', 'w:gz') as archive:
        archive.add(snapshot, arcname='old/settlement.json')
    first = inventory([source], out)
    second = inventory([source], out)
    assert first['source_revisions'] == second['source_revisions']
    assert first['decoded_archive_member_bytes'] == snapshot.stat().st_size
    manifest, rows = materialize(out/'store', first['source_revisions'], out, 1300_000_000_000, 'a'*40)
    assert rows[0]['outcome'] == 1
    assert manifest['exclusions']['DUPLICATE_SOURCE_RECORD'] == 1


def test_forecast_scope_is_exactly_three_models():
    pytest.importorskip("sklearn")  # Required in the isolated causal-learning CI job.
    from research.learning.models import FAMILIES
    assert FAMILIES == ('pm', 'logistic_offset', 'boosted_offset')


def test_legacy_pending_opportunity_is_kept_without_synthetic_label():
    from research.learning.dataset import pending_legacy
    cut = {'observed_wall_ns': 1_100_000_000_000, 'market_probability': .5,
           'oracle_value': 100., 'reference_value': 100., 'observed_tte_seconds': 200.,
           'external_features': {'composite_price': 100., 'age_ns': 10_000}}
    origin = {'evidence_semantics_version': 'external-fair-settlement-evidence-v2',
        'paper_only': True, 'authenticated_execution': False, 'real_order_submission': False,
        'execution_authority': 'SHADOW_ZERO_AUTHORITY', 'market_id': 'm', 'forecast_id': 'f',
        'market_mid_source': 'LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH',
        'rich_feature_cut': cut, 'rich_feature_sha256': digest(canonical(cut)),
        'observed_ms': 1_100_000, 'reference_version': 1000}
    row = pending_legacy(origin, 1_400_000_000_000)
    assert row['outcome'] is None and row['label_state'] == 'PENDING'
    assert row['training_eligible'] is False
    origin['rich_feature_sha256'] = '0'*64
    assert pending_legacy(origin, 1_400_000_000_000) is None


@pytest.mark.parametrize('mode,expected', [('FULL','PARTIAL_FILL'), ('DECISIONS','CENSORED'), ('DECISION_WINDOWS','CENSORED')])
def test_arrival_replay_requires_proven_continuous_capture(tmp_path, mode, expected):
    pytest.importorskip("sklearn")
    from research.learning.catalog import CLOSED
    source=tmp_path/'source'; source.mkdir(); out=tmp_path/'private'
    common={'capture_mode':mode,'connection_epoch':1,'paper_terms_sha256':'b'*64,
        'fee_rate':0.,'fee_exponent':1.,'fee_source':'TEST_PUBLIC_TERMS',
        'paper_venue_delay_ns':0,'minimum_order_microunits':1_000_000,'book_version':1}
    rows=[native(**common,kind=1,sequence=1,observed_monotonic_ns=199_990_000_000),
          native(**common,sequence=2,observed_monotonic_ns=200_000_000_000),
          native(**{**common,'book_version':2},kind=3,sequence=3,observed_monotonic_ns=200_000_500_000,
                 receive_monotonic_ns=200_000_500_000,ask_quantity=1_000_000),label()]
    write(source/'capture.jsonl',rows)
    closure={'schema':CLOSED,'server_id':'test','run_id':'run','capture_id':'capture',
             'closed':True,'healthy':True,'last_sequence':3,'watermark_monotonic_ns':202_000_000_000}
    (source/'closure.json').write_text(json.dumps(closure,indent=2))
    c=inventory([source],out)
    _, rows=materialize(out/'store',c['source_revisions'],out,1300_000_000_000,'a'*40,
        execution_scenario={'transport_delay_ns':1_000_000,'quantity_shares':2.,'execution_cost_per_share':.005})
    e=rows[0]['execution']
    if expected == 'CENSORED':
        assert e is None or e['state'] == expected
        assert rows[0]['execution_censored'] is True
    else:
        assert e['state'] == expected and e['quantity'] == 1
        assert e['net_pnl'] == pytest.approx(.485)
        assert e['cost_assumption']['measured'] is False


def test_actual_native_writer_code_sha_is_accepted_without_legacy_model_sha():
    row=native(code_sha='b'*40);row.pop('model_sha')
    assert native_example(row)['code_sha']=='b'*40


def test_daily_growth_distinguishes_new_signals_new_labels_and_shrinkage(tmp_path):
    from research.learning.daily import growth_receipt
    old=[dict(signal_id='old',market_id='a',decision_ns=1,training_eligible=False,outcome=None)]
    (tmp_path/'dataset_manifests').mkdir();(tmp_path/'datasets').mkdir()
    (tmp_path/'dataset_manifests'/'prior.json').write_text(json.dumps({'views':{'signals':{'sha256':'view'}}}))
    (tmp_path/'datasets'/'view.jsonl').write_text(json.dumps(old[0])+'\n')
    rows=[dict(old[0],training_eligible=True,outcome=1),dict(old[0],signal_id='new',market_id='b',decision_ns=2)]
    manifest=dict(unique_markets=2,unresolved=1,first_decision_ns=1,last_decision_ns=2)
    value=growth_receipt(tmp_path,{'dataset_sha256':'prior'},manifest,rows)
    assert value['training_rows']==1 and value['new_observations']==1 and value['new_resolved_observations']==1
    assert not value['historical_data_shrank']
    value=growth_receipt(tmp_path,{'dataset_sha256':'prior'},manifest,rows[1:])
    assert value['historical_data_shrank'] and value['lost_observations']==1


def test_settlement_polling_continues_after_daily_receipt_and_does_not_backdate(tmp_path,monkeypatch):
    import platform
    from research.learning import daily, settlements
    (tmp_path/'research_host.json').write_text(json.dumps(dict(schema='v7_research_host_v1',hostname=platform.node(),role='RESEARCH_ONLY')))
    source=tmp_path/'source';source.mkdir()
    (source/'population.json').write_text(json.dumps({'markets':['m'],'updated_ns':daily.time.time_ns()}))
    (tmp_path/'settings.json').write_text(json.dumps({'source_roots':[str(source)],'fetch_public_settlements':True,'collection_interval_seconds':0}))
    receipts=tmp_path/'receipts';receipts.mkdir()
    date,_=daily.midnight();receipt={'training_date':date,'result':'ALREADY_FROZEN','dataset_sha256':'missing-view'}
    (receipts/(date+'.json')).write_text(json.dumps(receipt))
    calls=[]
    monkeypatch.setattr(settlements,'collect',lambda markets,root: calls.append(set(markets)) or {'resolved':0})
    assert daily.run(tmp_path)==receipt
    assert daily.run(tmp_path)==receipt
    assert calls==[{'m'},{'m'}]
    assert json.loads((tmp_path/'collection_status.json').read_text())['information_ns']>daily.midnight()[1]
