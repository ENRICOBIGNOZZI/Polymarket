# Independent cross-venue prediction-market engine

The repository contains two operationally independent trading engines under one portfolio-level risk authority:

```text
                         Portfolio Supervisor
                         /                  \
                        /                    \
          Polymarket Alpha Engine     Cross-Venue Arb Engine
                                             |
                            Polymarket / Limitless / Kalshi
```

The incumbent Polymarket champion is unchanged. The cross-venue engine is a separate binary, process, configuration, state directory, paper ledger and failure domain. A cross-venue process failure must not terminate the incumbent alpha process. The supervisor publishes capital and new-exposure gates for both engines.

## Implemented boundary

`prediction_cross_venue_arb` implements:

- Polymarket, Limitless and Kalshi market discovery;
- authenticated Limitless HMAC-SHA256 requests;
- authenticated Kalshi RSA-PSS/SHA-256 requests;
- venue-normalized YES and NO order books;
- candidate contract matching for review;
- an explicit, reviewed pair manifest;
- same-event and complement-event two-leg arbitrage;
- displayed-depth walking, slippage, fee reserves and latency reserves;
- supervisor capital gating, pair cooldowns and persistent paper accounting;
- atomic `runtime_status.json`, candidate, opportunity and bundle outputs.

Authenticated order submission is deliberately not compiled into this research branch. Credentials are used for signed market-data/account-health calls. `LIMITLESS_EXECUTION_ENABLED=0` and `KALSHI_EXECUTION_ENABLED=0` remain mandatory. A later live adapter must pass the repository's OOS, risk, reconciliation and explicit-promotion process.

## Credentials

No secret belongs in Git, a JSON config or a command-line argument. The engine reads:

```text
~/.config/polymarket/venues.env
~/.config/polymarket/kalshi-private-key.pem
```

Install or update the Kalshi key without printing it:

```bash
bash scripts/install_cross_venue_credentials.sh
```

The installer preserves existing Limitless fields, validates the RSA key with OpenSSL and applies mode `600` to both the PEM and environment file.

Expected environment names:

```text
LIMITLESS_TOKEN_ID
LIMITLESS_TOKEN_SECRET
LIMITLESS_ACCOUNT
LIMITLESS_EXECUTION_ENABLED=0

KALSHI_API_KEY_ID
KALSHI_PRIVATE_KEY_PATH
KALSHI_ENV=production|demo
KALSHI_EXECUTION_ENABLED=0
```

## Build and authentication check

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure

./build/prediction_cross_venue_auth_check \
  --config config/cross_venue.json \
  --credentials "$HOME/.config/polymarket/venues.env"
```

The authentication checker emits JSON containing only configuration booleans and HTTP status codes. It never prints token IDs, secrets, addresses, signatures or private-key material.

## Pair verification

Hard arbitrage is permitted only for rows in `config/cross_venue_pairs.csv` with `settlement_verified=true`.

```csv
pair_id,venue_a,market_a,venue_b,market_b,relation,settlement_verified,max_close_diff_seconds,notes
```

`relation` is one of:

- `same_event`: buy `YES_A + NO_B` or `NO_A + YES_B`;
- `complement_event`: buy `YES_A + YES_B` or `NO_A + NO_B`.

The candidate matcher writes `runs/cross_venue/candidate_matches.csv`, but a high text score is not settlement equivalence. Before setting `settlement_verified=true`, review at least the resolution source, threshold convention, observation timestamp, timezone, early-close rule, cancellation rule, invalid-market treatment and payout currency.

## Run one cycle

The supervisor must publish a gate first:

```bash
python3 scripts/portfolio_supervisor.py \
  --config config/portfolio_supervisor.json --once

./build/prediction_cross_venue_arb \
  --config config/cross_venue.json \
  --credentials "$HOME/.config/polymarket/venues.env" \
  --pairs config/cross_venue_pairs.csv \
  --run-dir runs/cross_venue \
  --once
```

## Run the integrated system

```bash
bash scripts/prediction_market_system_loop.sh
```

The top-level loop supervises three independent processes:

1. `portfolio_supervisor.py`;
2. the incumbent `paper_latest_loop.sh`;
3. `cross_venue_loop.sh`.

Set `PREDICTION_SYSTEM_START_ALPHA=0` when the incumbent alpha service is already managed elsewhere. Set `PREDICTION_SYSTEM_START_CROSS=0` for supervisor-only maintenance.

## Runtime state

```text
runs/
  paper_v4_live/               incumbent alpha state
  cross_venue/
    auth_check.json
    candidate_matches.csv
    opportunities.csv
    paper_bundles.csv
    paper_state.json
    runtime_status.json
  supervisor/
    capital_limits.json
    runtime_status.json
    state.json
  system/
    runtime_planes.csv
    events.csv
    *.log
```

The cross-venue paper ledger records capital as reserved until settlement. Expected locked profit is reported separately and is not treated as realized PnL.

## Fail-closed rules

New cross-venue paper exposure is rejected when any of the following holds:

- the supervisor gate is missing, stale or closed;
- global drawdown or the manual `runs/supervisor/KILL` switch is active;
- the pair is not settlement-verified;
- either executable ask is absent;
- displayed depth is insufficient;
- close times differ beyond the pair-specific tolerance;
- net edge fails after slippage, fee and latency reserves;
- the engine exceeds its capital allocation, bundle cap or cooldown.
