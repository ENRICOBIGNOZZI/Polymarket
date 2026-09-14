# V7 Hot-Path and Data-Plane Findings — 2026-09-14

This note records technical/causal observations only. No active-window PnL, markout, fill-quality, or frozen economic endpoint was inspected.

## Live findings

- Active runtime stayed exact SHA `aa5311a610980e944a09ee7a236e972ee3b364da`, PAPER-only, with `/healthz` green during the investigation.
- External fair was not REST-bound: the event-driven composite was healthy with three fresh venues; a sampled composite age was about 17 ms. Binance/Coinbase/Bybit spot L2 were LIVE.
- The PAPER router still launched with `--interval 1`. A technical cadence sample observed router decisions at roughly 1.8 s median, with a maximum around 2.8 s, while fair state changed multiple times between decisions.
- Canonical-account reconciliation took about 150 ms in a sampled status and was executed inside every router `step()` together with position/forecast maintenance.
- The router also retained a synthetic 100 ms arrival revalidation delay in its legacy PAPER exploration path.

## Causal-book / repricing evidence

- Recent PM repricing label coverage was only about 9–10% over 5–10 minute windows; roughly 90% of resolved origins were censored by book continuity/watermark requirements.
- Maker cohort rotation count reached 65 in about 5h50m, close to the configured five-minute minimum interval.
- Each material Maker rotation stops and restarts both markout and fillability observers. The fillability observer is also the producer of the canonical book tape used by repricing and lead/lag labels.
- A zero-authority `--fair-only` smoke test succeeded without any Maker selection file: 0 dropped events, 0 decoder failures, and a valid causal book tape.
- In a 25-second live-following technical coverage test, the dedicated fair-only tape supplied valid causal books at both origin and origin+250 ms for 48/49 repricing origins = 97.96% technical coverage. Prediction values and economic outcomes were not inspected.

## Host/resource findings

- The server has 10 physical CPU cores and 32 GiB RAM.
- During the live run, `v7_generate_economic_artifacts.py` was observed around 80–95% of one CPU core and roughly 9.7 GiB RSS; system load average was around 18.
- Disk usage was about 86% on the data volume.
- The live launcher runs retrospective reporting, attribution, horse-race, compaction, and permanent-evidence work on the same host as the execution/data plane.
- Full adaptive-universe discovery recently took roughly 90–115 seconds across about 18k markets. Exact-slug BTC M5 binding already exists in the settlement monitor, so the critical BTC M5 handoff does not need to depend on exhaustive discovery.

## Branch changes

1. Add a persistent `--fair-only` causal-book observer used by repricing and lead/lag research. It follows only the verified settlement pair and does not depend on Maker selection rotation.
2. Run the external-fair router on a 250 ms deadline schedule, while moving reconciliation/positions/forecasts to a 1 Hz maintenance phase in the same owner thread.
3. Fail closed if maintenance fails: no new entry decisions until a complete maintenance pass restores readiness.
4. Instrument L2 lineage loss by source: invalid full snapshot, invalid price change, invalid tick-size change, and price changes received after lineage is already lost. No decoder acceptance rule is relaxed.
5. Place retrospective analytics under macOS background scheduling plus lower CPU priority. Health-critical canonical economics is deliberately not backgrounded.

## Not changed yet

- The 100 ms synthetic PAPER arrival revalidation delay remains in the live/router code on this branch. Removing it should be integrated with the separately tested fresh-book keep-alive challenger rather than done independently.
- No live deployment is permitted before the current forward-window verdict.
- Fast-cancel is not being rewritten: current evidence favors spending engineering effort on entry cadence and causal-book continuity first.
