from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_hotpath_has_receive_decision_submission_ack_fill_clock_chain():
    event=(ROOT/'include/pm/v7_external_ingress.hpp').read_text() + (ROOT/'include/pm/v7_external_state.hpp').read_text()
    maker=(ROOT/'src/v7_maker_hft.cpp').read_text()
    oms=(ROOT/'src/v7_oms.cpp').read_text()
    executor=(ROOT/'src/v7_native_runtime_evidence.cpp').read_text()
    assert 'local_receive_monotonic_ns' in event
    for token in ('feature_ns','decision_ns','risk_ns','receive_to_intent_ns'):
        assert token in maker
    for token in ('submission_ns','wire_ns','ack_ns','live_ns'):
        assert token in oms
    assert 'FILL' in executor and 'simulated_arrival_monotonic_ns' in executor

def test_london_has_no_report_generation_process():
    launcher=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    for token in ('v7_generate_economic_artifacts.py','v7_profit_report.py','v7_economic_decision_report.py','matplotlib','plotly'):
        assert token not in launcher
    assert 'v7_canonical_economics.py' in launcher  # lightweight reconciliation only
