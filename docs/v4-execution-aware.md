# Polymarket V4: execution-aware paper architecture

V4 is a paper-only sidecar. It does not contain wallet credentials, authenticated order submission, cancel/replace calls, or real balance handling.

## Economic object

V4 does not rank a midpoint discrepancy. It ranks expected executable PnL:

\[
E[\Pi_i]
=
P_i(\mathrm{fill}\le H)
\left[
e_i + R_i^{rebate}+R_i^{liquidity}
-C_i-A_i^{selection}
\right].
\]

The four alpha sleeves remain separate:

1. **Structural** — binary parity, complete NegRisk baskets, and curated logical payoff floors.
2. **Terminal** — external evidence that explicitly estimates terminal event probability.
3. **Relative value** — robust factor residuals, usable only when a regime gate assigns sufficiently high mean-reversion probability.
4. **Maker economics** — spread/informational edge, fill probability, configured rewards, learned adverse-selection markout, and expected capital time.

No PCA residual is silently converted into a terminal probability.

## Deterministic event contract

Every normalized market state is appended to `events.jsonl`. A state contains:

- sequence and event timestamp;
- market, condition and event identifiers;
- complete recorded YES/NO book levels;
- liquidity, fee parameters and NegRisk state;
- external terminal signal and decayed confidence, when available;
- resolution state for held markets.

The canonical state hash is independent of input ordering. Decisions are written to `decisions.jsonl` with both the input hash and decision hash. Replaying the same normalized event stream under the same normalized configuration must produce zero hash mismatches.

```bash
./build-v4/polymarket_v4_replay \
  --config config/v4.paper.example.json \
  --events runs/v4_paper/events.jsonl \
  --expected runs/v4_paper/decisions.jsonl \
  --run-dir runs/v4_replay
```

The replay directory must differ from the source directory.

## Structural sleeve

Implemented long-only payoff identities:

- buy YES and NO when their all-in cost is below one;
- buy a complete NegRisk YES basket when midpoint completeness is plausible and all-in cost is below one;
- `A => B`: buy NO(A) and YES(B) when the basket costs below one;
- mutually exclusive `A,B`: buy NO(A) and NO(B) when the basket costs below one;
- curated exhaustive groups: buy every YES leg below one.

Curated relations live in `data/v4_relations.csv`. Completeness is deliberately conservative; an incomplete event set must not be treated as an arbitrage.

## Terminal sleeve

A terminal trade exists only when `data/external_signals.csv` supplies a probability and sufficient confidence. Microprice, PCA and semantic similarity are not terminal evidence. Entry edge is measured against the actual ask and reduced by fee, configured slippage and confidence-dependent uncertainty.

## Relative-value sleeve and regime gate

For each market, V4 computes a logit return and removes the cross-sectional median factor. The residual is standardized by a rolling median/MAD scale. A candidate is tradable only if

\[
|z_t|>z^\star
\quad\text{and}\quad
P(R_t=\mathrm{MR}\mid\mathcal I_t)>\pi^\star.
\]

The current transparent regime score uses lag-one autocorrelation, sign alternation, trend strength and jump concentration. It is intentionally conservative. This block is designed to be replaced by an OOS-trained classifier after the recorder has accumulated labels.

## Maker sleeve

A passive quote is scored by

\[
S_i=
\frac{
P_i(\mathrm{fill}\le H)
\left[
e_i+R_i^{rebate}+R_i^{liquidity}-A_i^{selection}
\right]
}{
p_i\,E[\tau_i]
}.
\]

The fill model uses spread ticks, touch depth, imbalance and momentum. The queue/partial-fill simulator is a **deterministic paper proxy**, not a claim that public snapshots reveal the true exchange queue. Latency delays order activation. Hash-derived uniforms make simulated fills reproducible rather than random across replays.

Maker rebates and liquidity rewards default to zero. They should be configured only from verified market/program data.

## Markout feedback

Each simulated buy fill schedules signed markouts

\[
M_h=p_{t+h}^{side}-p_{\mathrm{fill}},
\qquad
h\in\{1,5,30,60,300\}\text{ seconds}.
\]

Markouts are stored in `markouts.csv`. The adverse-selection model maintains feature-bucket and global running means. The maker score subtracts the predicted negative 30-second markout. Thus observed execution outcomes feed back into future decisions.

## Allocator and risk

Opportunities are ordered by sleeve priority and within-sleeve score:

\[
\text{structural}>\text{terminal}>\text{maker}>\text{relative value}.
\]

Sizing is clipped by:

- cash;
- per-trade or maker-order notional;
- market exposure;
- event exposure;
- gross exposure;
- remaining ex-ante drawdown-loss budget.

A 15% drawdown setting is an operating constraint, not a mathematical guarantee.

## Runtime outputs

Under the configured run directory:

- `events.jsonl` — deterministic normalized inputs;
- `decisions.jsonl` — alpha candidates, actions and hashes;
- `orders.jsonl` — maker order lifecycle and partial fills;
- `fills.jsonl` — simulated fills and settlements;
- `markouts.csv` — adverse-selection observations;
- `adverse_selection.csv` — learned markout state;
- `v4_state.json` — restartable paper account, orders, histories and pending markouts;
- `status.json` — latest account and decision snapshot;
- `config.normalized.json` — configuration contract used by the run.

## Validation standard

A green build proves software consistency, not profitability. Economic validation requires an untouched paper-live stream and OOS reporting of:

- opportunity count by sleeve;
- fill rate and calibration;
- markouts by state bucket;
- gross-to-net edge attrition;
- realized and marked PnL;
- turnover, capital time and drawdown;
- replay mismatch count;
- performance after all explicit costs.

Zero trades is a valid outcome.
