from __future__ import annotations

from pathlib import Path

import numpy as np

from research.walk_forward_v3 import multi_alpha_2h as m


def test_feature_asof_is_backward_only():
    index={
        "m":{
            "stamps":[100,200,300],
            "rows":[
                {"ready_ns":100,"features":{"x":1.0}},
                {"ready_ns":200,"features":{"x":2.0}},
                {"ready_ns":300,"features":{"x":3.0}},
            ],
        }
    }
    assert m.feature_asof(index,"m",99) is None
    assert m.feature_asof(index,"m",100)["features"]["x"]==1.0
    assert m.feature_asof(index,"m",250)["features"]["x"]==2.0
    assert m.feature_asof(index,"m",300)["features"]["x"]==3.0


def test_candidate_windows_are_exactly_two_hours_and_ignore_pnl_fields():
    base=1_800_000_000_000_000_000
    rows=[]
    for i in range(20):
        rows.append({
            "decision_ns":base+i*10*60*1_000_000_000,
            "decision_id":str(i),"market_id":"m"+str(i%4),
            "asset":"BTC" if i%2 else "ETH","horizon":"M5",
            "cash_pnl":999999.0 if i%3 else -999999.0,
        })
    windows=m.candidate_windows(rows)
    assert windows
    assert all(w["end_ns"]-w["start_ns"]==m.WINDOW_NS for w in windows)
    for w in windows:
        assert "cash_pnl" not in {k for k in w if k!="rows"}


def test_ridge_handles_missing_values_and_predicts_finite():
    records=[
        {"a":1.0,"b":2.0},
        {"a":2.0},
        {"a":3.0,"b":4.0},
        {"a":4.0,"b":5.0},
    ]
    model=m.Ridge(alpha=1.0).fit(records,[1.0,2.0,3.0,4.0])
    pred=model.predict([{"a":2.5},{"a":3.5,"b":4.5}])
    assert pred.shape==(2,)
    assert np.all(np.isfinite(pred))


def test_family_nesting_is_monotone():
    rows=[{"features":{
        "binance_return_100ms_bp":1.0,
        "external.return_250ms":2.0,
        "aggregate_trade_imbalance":3.0,
        "native_vol_fast":4.0,
    },"pair":{},"tte_ns":60_000_000_000,"signal_age_ns":1_000_000,
        "bid":.4,"ask":.41,"quantity":10.0,"direction":1}]
    nested,_=m.nested_family_keys(rows,{})
    prior=set()
    for name,_ in m.FAMILIES:
        current=set(nested[name])
        assert prior.issubset(current)
        prior=current


def test_program_is_paper_only_and_has_no_execution_transport():
    source=Path(m.__file__).read_text(encoding="utf-8")
    assert "real_order_submission" not in source or "No real execution" in source
    forbidden=("requests.post(","requests.get(","subprocess.run(","os.system(","place_order","cancel_order")
    assert not any(token in source for token in forbidden)


def test_delayed_features_do_not_leak_native_external_cut():
    row={
        "features":{"binance_return_100ms_bp":99.0},
        "market_id":"m","decision_ns":1_000_000_000,
        "pair":{},"tte_ns":60_000_000_000,"signal_age_ns":1_000_000,
        "bid":.4,"ask":.41,"quantity":10.0,"direction":1,
    }
    tape={"m":{"stamps":[800_000_000],"rows":[{"ready_ns":800_000_000,"features":{"external.return_100ms_bp":2.0}}]}}
    now=m.row_features(row,tape,0)
    delayed=m.row_features(row,tape,100)
    assert now["binance_return_100ms_bp"]==99.0
    assert "binance_return_100ms_bp" not in delayed
    assert delayed["external.return_100ms_bp"]==2.0
