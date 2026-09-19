from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_fast_cancel_latency_contract() -> None:
    launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
    native = (ROOT / "src/v7_crypto_settlement_engine.cpp").read_text()
    paper = (ROOT / "src/v7_native_paper_execution.cpp").read_text()
    cfg = json.loads((ROOT / "config/v7_crypto_execution_alpha.json").read_text())
    rule = cfg["execution_alpha"]["cancel"]["research_rule"]
    signal = json.loads((ROOT / "config/v7_crypto_signal_policy.json").read_text())

    assert "v7_global_portfolio_coordinator.py" not in launcher
    assert "polymarket_v7_authorized_maker_paper_executor" not in launcher
    assert "external_policy.external_cancel_shock_window_ns = 100'000'000LL" in native
    assert "external_policy.external_cancel_signal_ttl_ns = options.strict_signal_policy" in native
    assert "options.minimum_absolute_binance_return_bp" in native
    assert "options.minimum_absolute_confirmation_return_bp" in native
    assert "external_cancel_confirmation_venue" in native
    assert "advance_external_cancel_signal(receive_ns - 1, external_policy)" in native
    assert "authority.cancel_maker_quote" in native
    assert "paper_execution.request_cancel" in native
    assert "slot->cancel_deadline_ns = now_monotonic_ns + cancel_latency_ns_" in paper

    assert rule["shock_window_ms"] == 100
    assert rule["minimum_absolute_log_return_bp"] == 0.30
    assert rule["trigger_grid_ms"] == 25
    assert rule["maximum_signal_age_ms"] == 100
    assert cfg["paper_only"] is True
    assert cfg["real_order_submission"] is False
    assert signal["paper_only"] is True and signal["real_order_submission"] is False
    assert len(signal["contexts"]) == 30
    assert signal["contexts"]["SOL:M5"]["maximum_signal_age_ms"] != signal["contexts"]["SOL:M15"]["maximum_signal_age_ms"]
    assert signal["contexts"]["BNB:M5"]["confirmation_venue"] == "BYBIT"
    assert signal["contexts"]["DOGE:M5"]["minimum_binance_return_bp"] != signal["contexts"]["BTC:M5"]["minimum_binance_return_bp"]


if __name__ == "__main__":
    test_fast_cancel_latency_contract()
