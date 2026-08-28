import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_strategy_governance as g


def test_registry_covers_exactly_15_and_remains_paper_only():
    registry = g.Registry.load(ROOT / "config" / "v7_strategy_registry.json")
    assert len(registry.strategies) == 15
    assert {x.family for x in registry.strategies} == g.FAMILIES
    assert registry.paper_only
    assert not registry.authenticated_execution
    assert not registry.real_order_submission
    assert not registry.automatic_promotion
    assert max(x.authority for x in registry.strategies) <= g.Authority.PAPER


def test_promotion_never_auto_authorizes_real_money():
    registry = g.Registry.load(ROOT / "config" / "v7_strategy_registry.json")
    maker = next(x for x in registry.strategies if x.family == "professional_maker")
    evidence = g.Evidence(1000, True, True, True, True, 10.0, 2.0, 1.0, True, True, True)
    result = g.promotion_assessment(maker, evidence, 100)
    assert result["statistically_eligible"] is True
    assert result["promotion_authorized"] is False
    assert "explicit_real_money_operator_authorization_required" in result["blockers"]


def test_evidence_gate_rejects_ticks_disguised_as_independent_samples():
    evidence = g.Evidence(4, False, False, True, False, 1.0, -0.5, 0.25, False, False, False)
    eligible, blockers = evidence.eligible(12)
    assert not eligible
    assert "insufficient_independent_samples" in blockers
    assert "nonpositive_2x_cost_pnl" in blockers


def test_conflict_priority_is_risk_then_structural_then_alpha():
    rows = [
        g.Candidate("maker", "professional_maker", "MAKE", "MAKER", "e", ("m",), 10, 1, 1, 0),
        g.Candidate("arb", "hard_arb", "ARB", "STRUCTURAL_GUARANTEE", "e", ("m", "n"), 1, 1, 1, 0),
        g.Candidate("risk", "professional_maker", "CANCEL", "RISK", "e", ("m",), -1, 1, 1, 0),
    ]
    selected = g.resolve_conflicts(rows)
    assert [x.candidate_id for x in selected] == ["risk"]


def test_full_hard_priority_order_starts_with_global_kill():
    rows = [
        g.Candidate(name, "professional_maker", "CANCEL", purpose, "e", ("m",), -1, 1, 1, 0)
        for name, purpose in (
            ("maker", "PASSIVE_MAKER"),
            ("risk", "NORMAL_RISK_REDUCTION"),
            ("critical", "CRITICAL_CANCEL"),
            ("global", "GLOBAL_KILL"),
        )
    ]
    assert g.resolve_conflicts(rows)[0].candidate_id == "global"


def test_all_strategy_ids_are_explicit_in_hot_path_contract():
    text = (ROOT / "include" / "pm" / "v7_intent.hpp").read_text()
    for name in ("HardArbitrage", "CryptoSettlementFair", "CryptoInformedTaker", "Osint",
                 "SportsLatency", "CrossPlatform", "WalletIntelligence", "MarketOpen"):
        assert name in text


def test_canonical_runtime_preflight_validates_registry_and_forbids_live_authority():
    text = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    assert 'v7.get("strategy_registry")' in text
    assert 'registry.get("governance",{}).get("automatic_promotion") is False' in text
    assert '{"RESEARCH","SHADOW","PAPER"}' in text
