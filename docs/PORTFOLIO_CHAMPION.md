# Portfolio Champion

The deployed paper system has one versioned portfolio champion and two bounded engine planes:

```text
                         Portfolio Champion
                                  |
                         Portfolio Supervisor
                         /                  \
                        /                    \
          Polymarket Alpha Engine     Cross-Venue Arb Engine
                                             |
                            Polymarket / Limitless / Kalshi
```

## Authority hierarchy

`config/portfolio_champion.json` is the system-level manifest. It registers the supervisor, the incumbent alpha champion selected by `config/live_champion.json`, and the cross-venue engine.

The portfolio supervisor is authoritative for:

- global capital;
- per-engine allocation;
- combined conservative equity and drawdown;
- new-exposure admission;
- the global manual and drawdown kill state.

Engine-local cash/state files are attribution and venue-reconciliation subledgers. They cannot create additional portfolio capital. Their allowed capital is bounded by the current supervisor gate.

## Shared gate

The supervisor atomically publishes:

```text
runs/supervisor/capital_limits.json
```

with one entry for each registered engine. Both planes fail closed on this file:

- the alpha plane applies `scripts/apply_portfolio_gate.py` after building complete intent bundles and before the incumbent broker reads them;
- the cross-venue plane reads the same gate before any paper bundle is admitted.

Missing, malformed, stale or globally killed supervisor state forbids new exposure. Existing positions remain under their engine's venue-specific paper lifecycle and secondary risk guard.

## Independent failure domains

The alpha engine, cross-venue engine and supervisor run as separate processes because they have different data sources, latency behavior, venue reconciliation and restart requirements. A process crash must not corrupt another plane's state. Independent processes do not imply independent capital authority.

## Cross-venue safety boundary

The cross-venue engine currently implements authenticated market-data/account-health access and paper execution only. Authenticated order submission is not compiled. Hard-arbitrage paper admission additionally requires an explicit row in `config/cross_venue_pairs.csv` with `settlement_verified=true` after review of resolution source, clock, threshold, cancellation and invalid-market rules.

The initial pair manifest intentionally contains no active verified pair.

## Deployment

All planes are part of the same exact repository revision. Integration, validation and deployment retain the existing sequence:

```text
integration merge -> exact-SHA CI/monitoring/live-paper validation
                  -> paper-validated -> deployed HEAD
```

No engine plane is deployed from a research branch, and no local credential is committed to Git.
