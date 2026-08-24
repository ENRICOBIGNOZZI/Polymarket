# All-Polymarket Engine

The V4 paper champion remains one risk-controlled portfolio, but opportunity discovery is no longer designed around a small fixed market universe.

## Universe

`PolymarketApi::discover_markets()` uses Gamma `GET /markets/keyset` and treats `limit=0` as unbounded discovery. The all-market sidecar independently inventories every active, open, order-book-enabled, accepting-order market and writes:

- `runs/paper_v4_live/all_market/universe.csv`
- `runs/paper_v4_live/all_market/universe_status.json`

The inventory is split into three compute tiers:

- Tier 0: every tradable market, metadata only;
- Tier 1: liquidity >= $20 by default, event-driven public market WebSocket and cheap structural checks;
- Tier 2: liquidity >= $100 by default, eligible for slower historical/statistical models.

The thresholds are compute-routing thresholds, not alpha thresholds. They do not authorize a trade.

## Event-driven fast layer

`polymarket_fast_arb_shadow` subscribes to the public market WebSocket and evaluates affected markets on each book event. The runtime supports `--markets 0`, which means all markets passing the configured minimum liquidity filter. Its existing safety policy remains mandatory:

- mode is `shadow`;
- `real_order_submission=false`;
- net edge includes explicit slippage and latency penalties;
- the process refuses a policy that enables real order submission.

The sidecar periodically recycles the feed so newly listed markets enter the universe without restarting the paper champion.

## Larger slow layer

The persistent V4 loop now defaults to:

- discovery: 3,000 markets;
- B1 historical universe: 800;
- B2 PCA universe: 400;
- scanner output: top 500;
- broker candidate bundles: up to 500;
- B1/B2 refresh: every 5 minutes;
- direct microstructure maker scan: 1,000 markets;
- structural scan: 3,000 markets;
- rewards shadow scan: 5,000 markets;
- universal terminal scan: 1,000 markets.

Every value is environment-configurable so the system can expand progressively without another code change. Candidate capacity is intentionally much larger than portfolio capacity.

## Global opportunity book

`scripts/build_global_opportunity_book.py` combines the event-driven fast structural layer, B1 pair stat-arb, coherent B2 PCA hedges, and fresh universal terminal signals into a common ranked book. The terminal sleeve contributes the graph, semantic, external and other active V4 experts through the existing executable-price, fee, slippage, uncertainty and sizing calculations. Append-only terminal observations older than the freshness window are rejected.

The book writes up to 1,000 research candidates by default:

- `global_opportunities.csv`: positive-raw-edge research frontier, including non-executable diagnostics;
- `global_trade_candidates.csv`: candidates that are positive after their source-specific executable cost model and admission logic;
- `global_opportunity_status.json`: counts, source mix and best-edge diagnostics.

The opportunity book does not bypass the broker. The existing portfolio constraints, drawdown kill switch, market/event concentration limits, cash reservation, leg-risk controls and fill model remain authoritative.

## Account integration

An account is not required for public market discovery, books, prices or the market WebSocket. `scripts/polymarket_account_readonly.py` adds optional account-specific reconciliation using L2 authentication. It reads both open orders and authenticated trade-history evidence so future paper-fill calibration can be compared with exchange-confirmed account activity.

Credentials are never committed. They are read only from:

- `POLYMARKET_ADDRESS`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_PASSPHRASE`

The adapter performs GET requests only, never prints credential values, and refuses non-GET signing. It can therefore be enabled before any authenticated execution adapter exists.

## Expansion rule

The intended progression is to expand the search frontier rather than lower the economic threshold:

1. increase market coverage and candidate capacity;
2. increase event-driven and cross-market relations;
3. partition expensive historical models into event/semantic/statistical clusters;
4. promote only candidates with positive realized paper evidence after fees, slippage, queue/fill risk and adverse selection;
5. keep real-money execution separately gated.

More candidates are allowed. More risk is not implied.
