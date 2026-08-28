from __future__ import annotations

import json
from pathlib import Path


def test_dashboard_contract():
    path = Path(__file__).resolve().parents[1] / "monitoring" / "grafana" / "dashboards" / "polymarket-v7-maker-lab.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    assert dashboard["uid"] == "polymarket-v7-maker-lab"
    assert "Microstructure" in dashboard["title"]
    raw = path.read_text(encoding="utf-8")
    for metric in (
        "polymarket_maker_lab_segment_filled_orders",
        "polymarket_maker_lab_segment_markout_pnl_usd",
        "polymarket_maker_lab_conditional_markout_pnl_usd",
        "polymarket_maker_lab_market_realized_pnl_usd",
    ):
        assert metric in raw
    for word in ("toxicity", "queue", "ofi", "inventory", "spread", "imbalance", "latency", "reward"):
        assert word in raw.lower()
