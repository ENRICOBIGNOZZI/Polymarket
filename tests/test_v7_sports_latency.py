import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_sports_latency as s


def mapping():
    return s.ContractMapping("m", "g", s.Sport.SOCCER, "MATCH_WINNER", "HOME", "league", "h", True)


def feed(seq=1, **state):
    base = dict(pregame_logit=0.0, score_home=1, score_away=0, remaining_seconds=600,
                red_cards_home=0, red_cards_away=0)
    base.update(state)
    return s.SportsFeedEvent(f"e{seq}", "g", seq, "SCORE", "league", True, 1000+seq, 1010+seq, base)


def test_verified_state_transition_produces_shadow_action():
    guard = s.FeedGuard("g", 1000); guard.apply(feed(), 1100)
    result = s.decide(mapping(), guard, now_ms=1100, pm_bid=.45, pm_ask=.50,
                      uncertainty=.03, executable_cost=.01, minimum_edge=.01)
    assert result.probability > .5 and result.action == "TAKE" and result.shadow_only


def test_sequence_gap_and_unverified_feed_fail_closed():
    guard = s.FeedGuard("g", 1000); guard.apply(feed(), 1100); guard.apply(feed(3), 1200)
    result = s.decide(mapping(), guard, now_ms=1200, pm_bid=.4, pm_ask=.5,
                      uncertainty=.02, executable_cost=0, minimum_edge=0)
    assert result.action == "NOTHING" and result.blocker == "feed_gap"


def test_contract_semantics_are_not_reused_for_totals():
    bad = s.ContractMapping("m", "g", s.Sport.SOCCER, "TOTAL", "HOME", "league", "h", True)
    guard = s.FeedGuard("g", 1000); guard.apply(feed(), 1100)
    assert s.decide(bad, guard, now_ms=1100, pm_bid=.4, pm_ask=.5,
                    uncertainty=.02, executable_cost=0, minimum_edge=0).action == "NOTHING"
