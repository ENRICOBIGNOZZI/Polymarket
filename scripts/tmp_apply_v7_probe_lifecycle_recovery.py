#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path

SOURCE_COMMIT = "df227d8b38d995340c452d6b28399ccb09a54c78"
SOURCE = Path("scripts/v7_external_fair_paper_router.py")
text = SOURCE.read_text(encoding="utf-8")

constant = '                "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",\n'
dynamic = '                "market_mid_source": metadata.get("market_mid_source"),\n'
if constant in text:
    text = text.replace(constant, dynamic, 1)
elif dynamic not in text:
    raise SystemExit("durable restore source precondition unavailable")

layout = '''        self.state["counterfactual_fills"] = len(fills)
        self.state["candidates"] = len(candidate_ids)
        self.state["opportunity_sets"] = len(opportunity_ids)
        self.state["counterfactual_realized_pnl"] = sum(
'''
normalized = '''        self.state["counterfactual_fills"] = len(fills)
        self.state["counterfactual_realized_pnl"] = sum(
'''
if layout in text:
    text = text.replace(layout, normalized, 1)
    tail = '''            for row in fill_finals.values()
        )
        self.state["traded_markets"] = sorted({
'''
    relocated = '''            for row in fill_finals.values()
        )
        self.state["candidates"] = len(candidate_ids)
        self.state["opportunity_sets"] = len(opportunity_ids)
        self.state["traded_markets"] = sorted({
'''
    if tail not in text:
        raise SystemExit("durable restore reduction tail unavailable")
    text = text.replace(tail, relocated, 1)
elif normalized not in text:
    raise SystemExit("durable restore counter precondition unavailable")

start = text.find("    def order_size(self, row: dict[str, Any]) -> float:\n")
end = text.find("    def common(self, status: dict[str, Any], row: dict[str, Any], order_id: str, size: float) -> dict[str, Any]:\n", start)
if start < 0 or end < 0:
    raise SystemExit("order_size function boundaries unavailable")
legacy_order_size = '''    def order_size(self, row: dict[str, Any]) -> float:
        book: Book = row["book"]
        ask = float(row["ask"])
        fee = float(row["fee_per_share"])
        execution_risk = float(row["execution_risk"])
        max_depth_fraction = min(1.0, max(0.0, float(self.policy.get("max_depth_fraction", 0.5))))
        depth_survival = min(1.0, max(0.0, float(self.policy.get("depth_survival_fraction", 0.75))))
        depth_fraction = min(max_depth_fraction, depth_survival)
        visible = book.asks[0][1] if book.asks else 0.0
        if row.get("paper_bootstrap_probe") is True:
            if self.probe_policy is None:
                return 0.0
            available_notional = min(
                float(self.state.get("starting_capital") or 0.0)
                * float(self.probe_policy["max_capital_fraction"]),
                float(self.probe_policy["max_notional_usd"]),
                float(self.probe_policy["max_loss_usd"]),
            )
        else:
            fraction_key = "max_market_capital_fraction" if self.model_mature else "immature_exploration_capital_fraction"
            fraction = float(self.policy.get(fraction_key, 0.02 if self.model_mature else 0.0025))
            available_notional = max(0.0, float(self.state["starting_capital"]) * fraction)
        unit_budget_cost = max(1e-9, ask + fee + execution_risk)
        size = min(visible * depth_fraction, available_notional / unit_budget_cost)
        size = math.floor(size * 100.0 + 1e-9) / 100.0
        if size + 1e-9 < book.min_order_size:
            return 0.0
        return size

'''
text = text[:start] + legacy_order_size + text[end:]

c2d_size_reject = '''        size = self.order_size(row)
        if size <= 0.0:
            self.last_attempt_reason = "BELOW_MINIMUM_EXECUTABLE_SIZE"
            return False'''
legacy_size_reject = '''        size = self.order_size(row)
        if size <= 0.0:
            self.last_attempt_reason = "INVALID_SIZE"
            return False'''
if c2d_size_reject in text:
    text = text.replace(c2d_size_reject, legacy_size_reject, 1)
elif legacy_size_reject not in text:
    raise SystemExit("minimum-size rejection precondition unavailable")

c2d_snapshot = '''            "robust_candidates": len(robust_rows),
            "probe_candidates": len(probe_rows),
            "candidate_mode": "ROBUST" if robust_rows else ("PAPER_BOOTSTRAP_PROBE" if probe_rows else "NONE"),
'''
legacy_snapshot = '''            "robust_candidates": len(robust_rows),
            "probe_candidate_count": len(probe_rows),
            "candidate_count": len(rows),
            "candidate_mode": "ROBUST" if robust_rows else ("PAPER_BOOTSTRAP_PROBE" if probe_rows else "NONE"),
'''
if c2d_snapshot in text:
    text = text.replace(c2d_snapshot, legacy_snapshot, 1)
elif legacy_snapshot not in text:
    raise SystemExit("decision snapshot precondition unavailable")

SOURCE.write_text(text, encoding="utf-8")
original = subprocess.check_output(
    ["git", "show", f"{SOURCE_COMMIT}:scripts/tmp_apply_v7_probe_lifecycle_recovery.py"],
    text=True,
)
namespace = {"__name__": "__main__", "__file__": str(Path(__file__).resolve())}
exec(compile(original, str(Path(__file__).resolve()) + ":pinned", "exec"), namespace)
