from pathlib import Path
import sys
from urllib.parse import urlsplit, parse_qs
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from v7_exact_arb_exchange_universe import discover_keyset_events


def test_follows_actual_cursor_beyond_old_offset_cap():
    requests=[]
    def fetch(url, timeout):
        query=parse_qs(urlsplit(url).query)
        assert urlsplit(url).path=="/events/keyset"
        assert "offset" not in query and "cursor" not in query
        page=int(query.get("after_cursor",["0"])[0]);requests.append(page)
        return {"events":[{"id":str(page*100+i)} for i in range(100)],
                "next_cursor":str(page+1) if page<24 else None}
    rows,info=discover_keyset_events("https://example.invalid",1,100,30,fetch)
    assert len(rows)==2500 and requests==list(range(25))
    assert info["discovery_exhaustive"] and not info["point_in_time_consistent"]


@pytest.mark.parametrize("bad", [{},[],{"events":[],"next_cursor":"advance"},
    {"events":[{"id":"1"}],"next_cursor":True},
    {"events":[{"id":"1"}],"next_cursor":"x"*4097}])
def test_malformed_or_nonadvancing_page_rejected(bad):
    with pytest.raises(ValueError):discover_keyset_events("https://example.invalid",1,2,2,lambda *_:bad)


def test_cursor_cycle_and_repeated_rows_fail_closed():
    for pages in ([{"events":[{"id":"1"}],"next_cursor":"a"},
                   {"events":[{"id":"2"}],"next_cursor":"a"}],
                  [{"events":[{"id":"1"}],"next_cursor":"a"},
                   {"events":[{"id":"1"}],"next_cursor":None}]):
        iterator=iter(pages)
        with pytest.raises(ValueError):discover_keyset_events("https://example.invalid",1,2,3,lambda *_:next(iterator))


def test_page_budget_does_not_claim_exhaustive():
    rows,info=discover_keyset_events("https://example.invalid",1,2,1,
        lambda *_:{"events":[{"id":"1"}],"next_cursor":"more"})
    assert len(rows)==1 and not info["discovery_exhaustive"] and info["pagination_loop_guard_hit"]
