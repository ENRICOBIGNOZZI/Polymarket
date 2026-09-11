from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_fast_cancel_latency_contract() -> None:
    launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
    coordinator = (ROOT / "scripts/v7_global_portfolio_coordinator.py").read_text()
    external = (ROOT / "src/v7_external_venue_runtime.cpp").read_text()
    executor = (ROOT / "src/v7_authorized_maker_paper_executor.cpp").read_text()
    cfg = json.loads((ROOT / "config/v7_crypto_execution_alpha.json").read_text())
    rule = cfg["execution_alpha"]["cancel"]["research_rule"]
    assert "--fast-cancel-interval 0.005" in launcher
    assert 'default=0.005' in coordinator
    assert "std::chrono::milliseconds(5)" in external
    assert "kFullStatusPublishIntervalNs = 25'000'000LL" in external
    assert "std::chrono::milliseconds(5)" in executor
    assert "executor_poll_interval_ms\"] = 5" in executor
    assert rule["shock_window_ms"] == 100
    assert rule["minimum_absolute_log_return_bp"] == 0.30
    assert rule["trigger_grid_ms"] == 25
    assert rule["maximum_signal_age_ms"] == 100
    assert cfg["paper_only"] is True
    assert cfg["real_order_submission"] is False


if __name__ == "__main__":
    test_fast_cancel_latency_contract()
