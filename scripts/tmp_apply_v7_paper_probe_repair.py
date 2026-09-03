#!/usr/bin/env python3
"""One-shot repair for venue-feasible, correctly accounted BTC M5 PAPER probes."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_config() -> None:
    path = ROOT / "config/v7_external_fair.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    probe = value["paper_exploration_probe"]
    probe.update({
        "max_capital_fraction": 0.0025,
        "max_notional_usd": 10.0,
        "max_loss_usd": 10.0,
        "minimum_model_market_disagreement": 0.05,
    })
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def patch_router() -> None:
    path = "scripts/v7_external_fair_paper_router.py"
    replace_once(
        path,
        '''        "max_capital_fraction": (0.00001, 0.0005),
        "max_notional_usd": (0.25, 2.0),
        "max_loss_usd": (0.25, 2.0),''',
        '''        "max_capital_fraction": (0.00001, 0.0025),
        "max_notional_usd": (0.25, 10.0),
        "max_loss_usd": (0.25, 10.0),''',
    )
    replace_once(
        path,
        '''        current_positions.update(restored_positions)
        self.state["positions"] = current_positions
''',
        '''        current_positions.update(restored_positions)
        self.state["positions"] = current_positions
        committed = sum(
            float(position.get("entry_cost") or 0.0)
            + float(position.get("entry_fee") or 0.0)
            for position in current_positions.values()
            if isinstance(position, dict) and not position.get("settled")
        )
        self.state["cash"] = max(
            0.0,
            float(self.state.get("starting_capital") or 0.0)
            + float(self.state.get("counterfactual_realized_pnl") or 0.0)
            - committed,
        )
''',
    )
    replace_once(
        path,
        '''        virtual_equity = starting_capital = float(self.state.get("starting_capital") or 0.0)
        virtual_equity += float(self.state.get("counterfactual_realized_pnl") or 0.0)
        virtual_equity += sum(
            float(position.get("executable_value") or 0.0)
            - float(position.get("entry_cost") or 0.0)
            - float(position.get("entry_fee") or 0.0)
            for position in positions.values() if not position.get("settled")
        )
''',
        '''        starting_capital = float(self.state.get("starting_capital") or 0.0)
        realized_pnl = float(self.state.get("counterfactual_realized_pnl") or 0.0)
        committed = sum(
            float(position.get("entry_cost") or 0.0)
            + float(position.get("entry_fee") or 0.0)
            for position in positions.values() if not position.get("settled")
        )
        cash = max(0.0, starting_capital + realized_pnl - committed)
        self.state["cash"] = cash
        virtual_equity = cash + sum(
            float(position.get("executable_value") or 0.0)
            for position in positions.values() if not position.get("settled")
        )
''',
    )
    replace_once(
        path,
        '''            "probe_candidates": int(self.state.get("probe_candidates") or 0),
            "probe_fills": int(self.state.get("probe_fills") or 0),
        })''',
        '''            "probe_candidates": int(self.state.get("probe_candidates") or 0),
            "probe_fills": int(self.state.get("probe_fills") or 0),
            "paper_exploration": {
                "enabled": self.probe_policy is not None,
                "authority": "COORDINATOR_RECEIPT_ONLY",
                "real_money_authority": False,
                "candidate_attempts": int(self.state.get("candidates") or 0),
                "selected_orders": int(self.state.get("counterfactual_fills") or 0),
                "selected_fills": int(self.state.get("counterfactual_fills") or 0),
                "probe_candidates": int(self.state.get("probe_candidates") or 0),
                "probe_fills": int(self.state.get("probe_fills") or 0),
                "open_positions": open_positions,
                "realized_pnl": realized_pnl,
                "cash": cash,
                "equity": virtual_equity,
                "max_capital_fraction": (
                    self.probe_policy.get("max_capital_fraction")
                    if self.probe_policy is not None else 0.0
                ),
                "max_notional_usd": (
                    self.probe_policy.get("max_notional_usd")
                    if self.probe_policy is not None else 0.0
                ),
                "max_loss_usd": (
                    self.probe_policy.get("max_loss_usd")
                    if self.probe_policy is not None else 0.0
                ),
            },
        })''',
    )


def patch_opportunity_contract() -> None:
    path = "scripts/v7_opportunity.py"
    replace_once(
        path,
        '''CRYPTO_CONTEXT_FIELDS = {
    "asset", "horizon", "contract_family", "settlement_semantic_hash",
    "authority", "research_only",
}
''',
        '''CRYPTO_CONTEXT_FIELDS = {
    "asset", "horizon", "contract_family", "settlement_semantic_hash",
    "authority", "research_only",
}
MAX_PAPER_PROBE_LOSS_USD = 10.0
''',
    )
    replace_once(
        path,
        "                or loss_cap > 2.0 + 1e-9\n",
        "                or loss_cap > MAX_PAPER_PROBE_LOSS_USD + 1e-9\n",
    )


def patch_tests() -> None:
    path = "tests/test_v7_paper_exploration_authority.py"
    replace_once(
        path,
        '        if mutation=="loss": value["exploration"]["maximum_probe_loss"]=2.01; value["exploration"]["probe_loss_cap"]=2.01\n',
        '        if mutation=="loss": value["exploration"]["maximum_probe_loss"]=10.01; value["exploration"]["probe_loss_cap"]=10.01\n',
    )
    replace_once(
        path,
        '''def test_live_router_has_distinct_probe_candidate_and_arrival_revalidation_paths():
''',
        '''def test_probe_budget_is_venue_feasible_at_five_share_minimum():
    import v7_external_fair_paper_router as router
    policy=json.loads((ROOT/"config/v7_external_fair.json").read_text())
    probe=router.validate_probe_policy(policy["paper_exploration_probe"])
    now=router.time.monotonic_ns()
    yes=router.Book("yes",((.49,100.0),),((.50,100.0),),.01,5.0,1000,1000,"y-mid")
    no=router.Book("no",((.49,100.0),),((.51,100.0),),.01,5.0,1000,1000,"n-mid")
    status={
        "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "contract":{"verified":True,"rules_hash_recognized":True},
        "settlement_reference":{"valid":True},
        "oracle":{"healthy":True,"continuity":"LIVE_CONTINUOUS"},
        "external":{"healthy":True},
        "market":{"yes_token":"yes","no_token":"no","fee_schedule":{"rate":0.0,"exponent":1,"takerOnly":True}},
        "fair":{"valid":True,"paper_exploration_bootstrap":True,"promotion_eligible":False,"real_money_authority":False,"probability_model_id":"btc_m5_same_oracle_diffusion_bootstrap_v1","probability_model_hash":"f"*64,"yes":.57,"lower":.45,"upper":.69,"tte_seconds":120.0,"calculated_monotonic_ns":now-1,"valid_until_monotonic_ns":now+10_000_000_000},
    }
    rows=router.paper_probe_candidates(status,{"yes":yes,"no":no},policy["taker"],probe)
    assert rows and rows[0]["outcome"]=="YES"
    with tempfile.TemporaryDirectory() as td:
        paper=router.PaperRouter(Path(td),"a"*40,ROOT/"config/v7_external_fair.json","https://clob.invalid","https://gamma.invalid")
        assert paper.order_size(rows[0]) >= 5.0
        assert probe["max_notional_usd"] == 10.0
        assert probe["max_loss_usd"] == 10.0


def test_live_router_has_distinct_probe_candidate_and_arrival_revalidation_paths():
''',
    )

    path = "tests/test_v7_external_fair_paper_router.py"
    replace_once(
        path,
        '''        assert status["counterfactual_pending_forecasts"] == 1
        assert status["model_mature"] is False
''',
        '''        assert status["counterfactual_pending_forecasts"] == 1
        assert status["paper_exploration"]["enabled"] is True
        assert status["paper_exploration"]["selected_orders"] == 1
        assert status["paper_exploration"]["selected_fills"] == 1
        assert status["paper_exploration"]["open_positions"] == 1
        assert status["paper_exploration"]["cash"] < status["paper_exploration"]["equity"]
        assert status["paper_exploration"]["max_notional_usd"] == 10.0
        assert status["model_mature"] is False
''',
    )
    replace_once(
        path,
        '''        assert failing.state["counterfactual_fills"] == 1
        assert len(failing.state["positions"]) == 1
''',
        '''        assert failing.state["counterfactual_fills"] == 1
        assert len(failing.state["positions"]) == 1
        assert failing.state["cash"] < failing.state["starting_capital"]
''',
    )


def remove_temporary_surfaces() -> None:
    for relative in (
        ".github/workflows/tmp-v7-paper-probe-economic-repair.yml",
        "scripts/tmp_apply_v7_paper_probe_repair.py",
    ):
        path = ROOT / relative
        if path.exists():
            path.unlink()


def regenerate_surface_audit() -> None:
    subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/v7_surface_classification.py"),
            "--repository-root",
            str(ROOT),
            "--output",
            str(ROOT / "artifacts/v7_unification/path_classification.json"),
        ],
        check=True,
        cwd=ROOT,
    )


def main() -> None:
    patch_config()
    patch_router()
    patch_opportunity_contract()
    patch_tests()
    remove_temporary_surfaces()
    regenerate_surface_audit()


if __name__ == "__main__":
    main()
