from research.walk_forward_v3 import a0_a5_maker_2h as a

def test_fixed_policy_contract():
    assert a.POLICIES == (
        "A0_BASELINE_NO_ADDED_ALPHA",
        "A1_EXTERNAL_MOMENTUM",
        "A2_PM_MICROSTRUCTURE",
        "A3_EXTERNAL_PLUS_PM",
        "A4_RESIDUAL_MEAN_REVERSION",
        "A5_FULL_EXECUTION_ALPHA",
    )
    assert tuple(a.LATENCIES)==(5,10,25,50,100,250)
    assert tuple(a.EXITS)==(100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000)

def test_information_sets_are_separated():
    assert "pm_response" not in a.A1_FAMILIES
    assert "cross_venue" not in a.A2_FAMILIES
    assert {"cross_venue","pm_response"} <= a.A3_FAMILIES
    assert a.A3_FAMILIES < a.A5_FAMILIES

def test_toxic_realized_fields_are_never_alpha_features():
    for name in ("realized_markout","future_return","label_fill","post_fill_pnl"):
        assert a.m.classify_source_feature(name) is None

def test_residual_rule_is_training_frozen():
    rows=[]
    for i in range(60):
        rows.append({"features":{
            "tape.pm.short_return_ticks":float(i),
            "external.binance_return_100ms_bp":float(i)*0.5,
        }})
    rule=a.fit_residual_rule(rows)
    assert rule["state"]=="READY"
    assert rule["abs_residual_q75"]>=0

def test_monotonicity_groups_are_shape_not_best_cell():
    rows=[
        {"policy":"A0","horizon_ms":250,"entry_latency_ms":5,"net_pnl_per_share":2.0},
        {"policy":"A0","horizon_ms":250,"entry_latency_ms":10,"net_pnl_per_share":1.0},
        {"policy":"A0","horizon_ms":250,"entry_latency_ms":25,"net_pnl_per_share":0.0},
    ]
    out=a.monotonicity(rows,"net_pnl_per_share","entry_latency_ms",("policy","horizon_ms"))
    assert out["A0|250"]["shape"]=="NONINCREASING"
