import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_osint_engine as o


def event(**kw):
    values = dict(event_id="e1", event_family="COURT_RULING", entity="case", source_id="court",
                  source_tier=o.SourceTier.PRIMARY, source_event_id="docket-1", root_lineage_id="root-1",
                  published_ts_ms=1000, received_ts_ms=1010, payload_hash="abc")
    values.update(kw); return o.RawEvent(**values)


def link(**kw):
    values = dict(event_family="COURT_RULING", market_family="court", direction=1,
                  causal_mechanism="official ruling resolves condition", mapping_version=1,
                  verified=True, verification_method="DETERMINISTIC")
    values.update(kw); return o.EventMarketLink(**values)


def model(**kw):
    values = dict(event_family="COURT_RULING", log_likelihood_ratio=1.0, independent_events=100,
                  trained_until_ms=900, frozen=True, oos_validated=True)
    values.update(kw); return o.LikelihoodModel(**values)


def test_primary_event_updates_probability_and_emits_shadow_take():
    result = o.update_probability(market_id="m", prior=.5, event=event(), link=link(), model=model(),
                                  decision_ts_ms=1100, uncertainty_log_odds=.1, pm_bid=.49, pm_ask=.51,
                                  executable_cost=.01, minimum_edge=.01)
    assert result.posterior > .5 and result.action == "TAKE" and result.shadow_only


def test_llm_mapping_and_future_event_fail_closed():
    result = o.update_probability(market_id="m", prior=.5, event=event(received_ts_ms=1200),
                                  link=link(verification_method="LLM"), model=model(), decision_ts_ms=1100,
                                  pm_bid=.4, pm_ask=.5)
    assert result.action == "NOTHING"
    assert "future_event_used" in result.blockers
    assert "llm_cannot_verify_mapping" in result.blockers


def test_deduplication_counts_copied_headlines_once():
    rows = o.deduplicate([event(event_id="a"), event(event_id="b", source_id="media")])
    assert len(rows) == 1
    assert o.edge_half_life({0: .1, 1: .08, 2: .05, 5: .01}) == 2.0
