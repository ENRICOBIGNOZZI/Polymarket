from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_execution_evidence_hardened as hardened


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def permissive_policy() -> dict:
    policy = hardened.base.default_policy()
    policy["bootstrap_samples"] = 100
    for row in policy["models"].values():
        row.update({
            "min_fills": 1,
            "min_pnl_observations": 1,
            "min_markout_observations": 0,
            "min_fill_rate": 0.0,
            "min_active_folds": 0,
            "min_positive_fold_fraction": 0.0,
            "max_bootstrap_pvalue": 1.0,
        })
    return policy


def test_audited_cost_sums_nonoverlapping_components() -> None:
    row = {"fee": "1.0", "slippage_cost": "2.0", "capital_time_cost": "0.5"}
    assert abs(hardened.audited_cost(row) - 3.5) < 1e-12
    aggregate = {"execution_cost": "7.0", "fee": "1.0", "slippage_cost": "2.0"}
    assert abs(hardened.audited_cost(aggregate) - 7.0) < 1e-12


def test_full_cost_coverage_uses_all_components_in_stress() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rows = [
            {"timestamp": "86401", "market_id": "m1", "action": "BUY_TAKER", "pnl": "0", "fee": "0.2", "slippage_cost": "0.3"},
            {"timestamp": "86402", "market_id": "m1", "action": "SELL_TAKER", "pnl": "2.0", "fee": "0.2", "slippage_cost": "0.3"},
            {"timestamp": "172801", "market_id": "m2", "action": "BUY_TAKER", "pnl": "0", "fee": "0.2", "slippage_cost": "0.3"},
            {"timestamp": "172802", "market_id": "m2", "action": "SELL_TAKER", "pnl": "2.0", "fee": "0.2", "slippage_cost": "0.3"},
        ]
        write_csv(root / "micro_taker" / "fills.csv", ["timestamp", "market_id", "action", "pnl", "fee", "slippage_cost"], rows)
        report = hardened.build_report(root, permissive_policy(), now=200000)
        row = report["models"]["micro_taker"]
        assert row["cost_audit"]["cost_observation_coverage"] == 1.0
        assert abs(row["cost_audit"]["audited_baseline_cost"] - 2.0) < 1e-12
        # Raw PnL 4, 1.5x stress subtracts another 0.5 * baseline cost 2.
        assert abs(row["stressed_net_pnl"] - 3.0) < 1e-12
        assert "cost_observation_coverage_gate" not in row["reason_codes"]


def test_missing_cost_for_realized_pnl_key_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rows = [
            {"timestamp": "86401", "market_id": "m1", "action": "SELL_TAKER", "pnl": "2.0", "fee": "0.1"},
            {"timestamp": "172801", "market_id": "m2", "action": "SELL_TAKER", "pnl": "2.0", "fee": ""},
        ]
        write_csv(root / "micro_taker" / "fills.csv", ["timestamp", "market_id", "action", "pnl", "fee"], rows)
        report = hardened.build_report(root, permissive_policy(), now=200000)
        row = report["models"]["micro_taker"]
        assert row["cost_audit"]["cost_observation_coverage"] == 0.5
        assert "cost_observation_coverage_gate" in row["reason_codes"]
        assert not row["paper_eligible"]
        assert row["state"] == "INSUFFICIENT_EVIDENCE"
        assert report["summary"]["capital_allocation_mutated"] is False


def test_cli_writes_hardened_report() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        policy = root / "policy.json"
        policy.write_text(json.dumps(permissive_policy()), encoding="utf-8")
        output = root / "out.json"
        markdown = root / "out.md"
        assert hardened.main(["--run-root", str(root), "--policy", str(policy), "--output", str(output), "--markdown", str(markdown), "--now", "200000"]) == 0
        value = json.loads(output.read_text(encoding="utf-8"))
        assert value["cost_accounting_contract"].startswith("realized_net_pnl")
        assert value["summary"]["cost_accounting"] == "audited"
        assert "Cost audit" in markdown.read_text(encoding="utf-8")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} hardened execution-evidence tests")
