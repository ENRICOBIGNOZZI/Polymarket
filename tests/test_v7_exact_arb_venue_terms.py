"""Public descriptors are source evidence, never a live matching attestation."""
import hashlib
from itertools import count
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from v7_exact_arb_venue_terms import CLOB, MAX_RESPONSE, collect, decode, identity, persist, project
from v7_unified_exact_arb_graph import GraphError

CONDITION = "0x"+"a"*64
TOKENS = ["1", "2"]


@pytest.fixture
def sources():
    return {
        "compact": {"c": CONDITION, "t": [{"t": t} for t in TOKENS], "mts": ".01", "mos": 5,
                    "fd": {"r": ".04", "e": 1, "to": True}},
        "market": {"condition_id": CONDITION, "tokens": [{"token_id": t} for t in TOKENS],
                   "active": True, "closed": False, "accepting_orders": True, "enable_order_book": True,
                   "minimum_tick_size": ".01", "minimum_order_size": 5, "seconds_delay": 0},
        "books": {t: {"asset_id": t, "market": CONDITION, "tick_size": ".01", "min_order_size": "5"} for t in TOKENS},
    }


def projection(sources):
    return project(CONDITION, TOKENS, **sources)


def collector(sources, fail=None, clock=None):
    urls = {CLOB+"/clob-markets/"+CONDITION: sources["compact"], CLOB+"/markets/"+CONDITION: sources["market"]}
    urls.update({CLOB+"/book?token_id="+t: b for t,b in sources["books"].items()})
    def fetch(url):
        if fail and fail in url: raise TimeoutError("public_source_timeout")
        return json.dumps(urls[url]).encode()
    return collect(CONDITION, TOKENS, fetch, clock or count(1).__next__, count(1).__next__)


def test_omitted_itode_uses_explicit_documented_schema_rule(sources):
    row = projection(sources)
    assert row["mandatory_taker_delay_ms"] == 0 and row["itode"] is False
    assert row["itode_source"] == "DOCUMENTED_OPENAPI_OMISSION_FALSE"
    assert row["minimum_order_shares"] == "5"
    assert row["fee_rate"] == "1/25"


def test_true_itode_projects_250ms_not_transport_latency(sources):
    sources["compact"]["itode"] = True
    row = projection(sources)
    assert row["mandatory_taker_delay_ms"] == 250
    assert row["pending_delay_cancelable"] is False
    assert row["fee_fill_fragmentation_verified"] is False


@pytest.mark.parametrize("itode", [False, True])
def test_sports_activation_and_combined_delays_are_not_guessed(sources,itode):
    sources["compact"]["itode"] = itode
    sources["market"]["seconds_delay"] = 3
    row = projection(sources)
    assert row["state"] == "UNVERIFIED" and row["mandatory_taker_delay_ms"] is None
    assert row["configured_sports_delay_seconds"] == 3


@pytest.mark.parametrize("value", [None, False, 1, "0"])
def test_unknown_order_age_semantics_are_not_silently_zero(sources,value):
    sources["compact"]["oas"] = value
    assert projection(sources)["mandatory_taker_delay_ms"] is None


@pytest.mark.parametrize("field,value", [("itode",None),("itode",0),("itode","false"),("fd",None),
                                        ("mts",None),("mos",None),("mos",0),("t",[]),("c","wrong")])
def test_bad_compact_terms_fail_closed(sources,field,value):
    sources["compact"][field] = value
    with pytest.raises(GraphError): projection(sources)


@pytest.mark.parametrize("value", [None, False, "0", -1, 61, 0.0])
def test_missing_or_untyped_sports_delay_is_not_zero(sources,value):
    sources["market"]["seconds_delay"] = value
    with pytest.raises(GraphError,match="seconds_delay_unknown"): projection(sources)


def test_missing_sports_delay_is_not_zero(sources):
    del sources["market"]["seconds_delay"]
    with pytest.raises(GraphError,match="seconds_delay_unknown"): projection(sources)


@pytest.mark.parametrize("value", [{}, {"r":0,"e":1}, {"r":None,"e":1,"to":True},
                                   {"r":0,"e":None,"to":True},{"r":0,"e":1,"to":False}])
def test_missing_fee_information_never_means_free(sources,value):
    sources["compact"]["fd"] = value
    with pytest.raises(GraphError): projection(sources)


def test_explicit_zero_rate_is_preserved(sources):
    sources["compact"]["fd"]["r"] = 0
    assert projection(sources)["fee_rate"] == "0"


@pytest.mark.parametrize("field,value", [("asset_id","2"),("market","0x"+"b"*64),
                                        ("min_order_size","1"),("min_order_size",None),("tick_size",".001")])
def test_book_identity_and_share_minimum_must_agree(sources,field,value):
    sources["books"]["1"][field] = value
    with pytest.raises(GraphError): projection(sources)


@pytest.mark.parametrize("field,value", [("active",False),("closed",True),("accepting_orders",None),
                                        ("enable_order_book",1),("archived",True)])
def test_closed_or_unknown_market_cannot_supply_supported_terms(sources,field,value):
    sources["market"][field] = value
    with pytest.raises(GraphError,match="market_not_open"): projection(sources)


def test_duplicate_or_reused_tokens_rejected(sources):
    sources["compact"]["t"] = [{"t":"1"},{"t":"1"}]
    with pytest.raises(GraphError,match="token_binding"): projection(sources)
    with pytest.raises(GraphError): identity(CONDITION,["1","1"])
    with pytest.raises(GraphError): identity(CONDITION,["1",str(2**256)])


@pytest.mark.parametrize("raw", [b'{"itode":true,"itode":false}', b'{"r":NaN}', b'{', b' '* (MAX_RESPONSE+1)])
def test_malformed_or_ambiguous_json_is_not_accepted(raw):
    with pytest.raises(ValueError): decode(raw)


def test_receipt_preserves_raw_sources_without_execution_authority_or_lease(sources,tmp_path):
    row = collector(sources)
    assert row["state"] == "OBSERVED_SUPPORTED_TERMS"
    assert row["valid_until_ns"] is None and row["venue_execution_verified"] is False
    assert row["causal_session_binding_verified"] is False
    assert row["scope"] == "NONATOMIC_PUBLIC_VENUE_METADATA_ONLY"
    assert len(row["requests"]) == 4
    for request in row["requests"]:
        assert hashlib.sha256(request["raw_response"].encode()).hexdigest() == request["raw_sha256"]
    path = persist(tmp_path,row)
    assert persist(tmp_path,row) == path
    path.write_text("corrupted")
    with pytest.raises(GraphError,match="immutable_conflict"): persist(tmp_path,row)


def test_failed_refresh_does_not_retain_previous_supported_terms(sources):
    first = collector(sources)
    failed = collector(sources,fail="/markets/")
    assert first["terms"] is not None
    assert failed["state"] == "UNVERIFIED" and failed["terms"] is None
    assert "public_source_timeout" in failed["reason"]
    assert len(failed["requests"]) == 2 and failed["requests"][-1]["state"] == "FAILED"
    assert first["snapshot_sha256"] != failed["snapshot_sha256"]


def test_clock_inversion_or_jump_prevents_terms_admission(sources):
    for stamps in ([10,9],[1,100000001]):
        row = collector(sources,clock=iter(stamps).__next__)
        assert row["state"] == "UNVERIFIED" and row["terms"] is None
        assert "collection_clock" in row["reason"]


def test_raw_mutation_invalidates_receipt_hash(sources,tmp_path):
    row = collector(sources)
    row["terms"]["mandatory_taker_delay_ms"] = 250
    with pytest.raises(GraphError,match="snapshot_digest"): persist(tmp_path,row)
