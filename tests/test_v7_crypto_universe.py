import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_crypto_universe as universe

SHA = "a" * 40
NOW = 1788262200


def config():
    return json.loads((ROOT / "config" / "v7_crypto_universe.json").read_text())


def registry():
    return json.loads((ROOT / "config" / "v7_crypto_settlement_markets.json").read_text())


def market(index=1, slug="btc-updown-5m-1788262200", **overrides):
    row = {
        "id": str(index), "slug": slug, "conditionId": f"c{index}",
        "clobTokenIds": [f"y{index}", f"n{index}"], "outcomes": ["Yes", "No"],
        "liquidityNum": 10000-index, "volume24hr": index+1,
        "outcomePrices": ["0.45", "0.55"], "bestBid": .44, "bestAsk": .46,
        "spread": .02, "lastTradePrice": .45, "active": True, "closed": False,
        "acceptingOrders": True, "events": [{"id": f"e{index}"}],
    }
    row.update(overrides); return row


def test_discovery_queries_only_registered_crypto_slugs():
    cfg=config(); reg=registry(); seen=[]
    def fake(url, timeout):
        assert timeout == cfg["source"]["request_timeout_seconds"]
        parsed=urlparse(url); assert parsed.path.endswith('/markets')
        slug=parse_qs(parsed.query)['slug'][0]; seen.append(slug)
        return [market(len(seen), slug=slug)]
    rows, stats=universe.discover_crypto(cfg,reg,now_s=NOW,fetcher=fake)
    contexts=sum(1 for row in reg['contexts'] if row.get('enabled') is True)
    expected=contexts*len(cfg['source']['window_offsets'])
    assert len(seen)==expected and len(rows)==expected
    assert stats['discovery_exhaustive'] is True and stats['pagination_loop_guard_hit'] is False
    assert all(any(slug.startswith(asset.lower()+'-updown-') for asset in ('BTC','ETH','SOL','XRP','DOGE','BNB')) for slug in seen)
    assert {row['asset'] for row in rows} == {'BTC','ETH','SOL','XRP','DOGE','BNB'}


def test_missing_unpublished_adjacent_window_is_not_a_global_scan_failure():
    rows,stats=universe.discover_crypto(config(),registry(),now_s=NOW,fetcher=lambda _u,_t: [])
    assert rows == [] and stats['discovery_exhaustive'] is True and stats['missing_markets'] > 0


def test_transient_request_is_retried_then_succeeds():
    calls={}
    def flaky(url,_timeout):
        calls[url]=calls.get(url,0)+1
        if calls[url]==1: raise OSError('transient')
        slug=parse_qs(urlparse(url).query)['slug'][0]
        return [market(len(calls),slug=slug)]
    rows,stats=universe.discover_crypto(config(),registry(),now_s=NOW,fetcher=flaky)
    assert rows and stats['request_retries'] > 0


def test_normalization_and_crypto_identity_are_preserved():
    raw=market(); raw['_crypto_context']={
        'asset':'BTC','horizon':'M5','horizon_seconds':300,'contract_family':'BTC_USD_UPDOWN_5M',
        'settlement_semantic_hash':'a'*64,'research_only':False,'authority':'SHADOW','window_start_unix':NOW,
    }
    row=universe.normalize_market(raw)
    assert row['midpoint']==.45 and row['asset']=='BTC' and row['horizon']=='M5'
    assert row['window_start_unix']==NOW and row['asset']=='BTC'


def test_tiers_cover_every_eligible_crypto_market():
    cfg=config(); cfg['resource_budget']['hot'].update({
        'websocket_asset_capacity':8,'assets_per_market':2,'memory_budget_bytes':100,
        'estimated_bytes_per_market':20,'cpu_budget_micros_per_second':100,
        'estimated_update_rate_hz_per_market':2,'estimated_cpu_micros_per_update':10})
    cfg['resource_budget']['warm'].update({'scan_time_budget_millis':9,'estimated_scan_millis_per_market':2,
        'memory_budget_bytes':100,'estimated_bytes_per_market':10})
    rows=[universe.normalize_market(market(i,slug=f'btc-updown-5m-{NOW+i}')) for i in range(12)]
    snap=universe.build_snapshot(rows,{'discovery_exhaustive':True},cfg,model_sha=SHA,timestamp_ms=1)
    assert snap['tier_counts']=={'HOT':4,'WARM':4,'COLD':4}
    assert len(sum(snap['tiers'].values(),[]))==12


def test_skip_reasons_and_safety_fail_closed():
    cfg=config(); rows=[market(1),market(2,acceptingOrders=False),market(3,liquidityNum=0),market(4,conditionId=''),market(5,clobTokenIds=[])]
    norm=[universe.normalize_market(x) for x in rows]
    snap=universe.build_snapshot(norm,{'discovery_exhaustive':True},cfg,model_sha=SHA,timestamp_ms=1)
    assert snap['eligible_markets']==1
    assert snap['paper_only'] is True and snap['authenticated_execution'] is False and snap['real_order_submission'] is False


def test_configuration_contract_is_crypto_only():
    cfg=config(); universe.validate_config(cfg)
    assert cfg['market_registry']=='config/v7_crypto_settlement_markets.json'
    assert 'structural' not in cfg['resource_budget']
