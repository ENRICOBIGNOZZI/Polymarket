# V7 Selective PnL Challenger

This challenger is a zero-authority research policy. It was created from historical PAPER evidence only and is intentionally not wired into the active runtime while the `aa5311a...` 8-hour Maker forward window is running.

## Economic diagnosis

The historical generalized Maker policy produced too many low-quality quotes: fill rate was roughly 2%, and canonical historical PAPER finals were strongly negative in aggregate. Independent forward evidence for the existing `btc-m5-external-cancel-v1` rule showed a large positive avoided-markout effect, so adverse selection is treated as a first-class cost rather than a post-trade diagnostic.

Short-horizon prediction contains information, but aggregate settlement PnL is not stable across historical confirmatory windows. The challenger therefore does not authorize broad directional trading and does not use post-hoc YES-only or 10-cent filters as live rules.

A stricter same-model historical check found no regime cell that was positive in at least four of five contiguous frozen-model windows with adequate per-window support. NO-side economics were negative in all five, but that asymmetry remains report-only until a new prospective boundary tests it.

## 1. Selective Maker

`quote_everywhere=false`. A Maker candidate is shadow-eligible only when all execution-alpha features are present, evidence is mature, the market survived selection, the fill-probability lower bound is strictly positive, toxic-fill probability stays below the existing 0.75 ceiling, conservative MAKE EV is strictly positive, and the external-cancel state is clear. Active external-cancel evidence preempts Maker risk; unknown state fails closed.

## 2. Selective directional lane

The primary prospective gate is +1 cent/share **after 2x fee plus risk allowance**, after arrival revalidation. Both YES and NO remain eligible. The 0.5c, 3c and 10c thresholds are frozen research arms, not alternative thresholds to choose after seeing the next window. The primary also requires verified settlement binding, fresh external features, a mature model, at least five visible shares, and decision-to-arrival no greater than 250 ms.

## 3. Entry latency

The target is p50 <=150 ms, p90 <=250 ms and p99 <=500 ms decision-to-arrival. Candidate scanning is fixed at 250 ms and synthetic revalidation sleep at zero for the challenger. These targets are motivated by observed economic decay between 100 and 500 ms, not by a generic HFT benchmark.

`v7_fast_entry_shadow.py` implements a separate zero-authority measurement path: it reads the already-published External Fair status, immediately requests one fresh complement-consistent CLOB book batch, re-runs the existing robust-candidate logic, and records the fresh-book latency. It has no imports or paths for the canonical ledger, spool or opportunity inbox. It is not wired into the active runtime.

## 4. External information

External information is primarily a risk-control edge for Maker: hash-bound, receive-time-causal external shocks can force CANCEL/WITHDRAW. Directional trading may use external data only as a feature and causal confirmation; an external signal alone never grants TAKE authority.

## 5. Regime map

The prospective grid reuses the previously registered bins for TTE, external disagreement, 100 ms external shock, spread, volatility and oracle distance. Book imbalance and side are added as descriptive dimensions. Regime results are diagnostics for the next research generation; no regime may be promoted from the same window in which it is discovered.

## Frozen next-window protocol

`config/v7_selective_pnl_forward_protocol.json` preregisters the next eight-hour study before any deployment. It fixes one-look analysis, a 60-second grace period, Maker and directional primary endpoints, evidence minima, latency SLOs, the external-cancel identity, the regime grid, and the rule that the observed NO-side asymmetry cannot affect selection or sizing without a later fresh boundary.

The protocol also explicitly forbids activation before the current Maker verdict and requires a separate post-verdict review, a new exact-SHA deployment and a fresh forward boundary. This prevents the development work in PR #939 from silently replacing the running experiment.

## Verification

On the isolated server worktree, the challenger passes focused policy, fast-entry and frozen-protocol tests plus Python bytecode compilation. The active runtime checkout is re-read after verification and must remain exact SHA `aa5311a610980e944a09ee7a236e972ee3b364da` until its current forward verdict is complete.

A boundary-condition defect found during local verification was fixed: threshold comparisons now use a `1e-12` numerical tolerance so an intended 0.5c or 1c edge is not rejected solely because IEEE floating-point represents it microscopically below the decimal threshold.

## Active-window isolation

PR #939 must remain draft and unmerged until the active `aa5311a...` Maker forward verdict is complete. During that window, only operational integrity may be inspected. The challenger branch, its fast-entry shadow and the next-window protocol are development artifacts; none may change the active policy, code SHA, execution behavior or evidence boundary.

## Technical latency-only benchmark during the active window

A dedicated latency-only mode was added specifically so entry-path mechanics can be measured without computing or writing model edge, candidate count, selected side, market probability, TTE, PnL or markout. Outputs were written under /tmp, not into the active experiment evidence tree.

Two short operational samples separated by a BTC M5 market handoff produced a useful systems result. On market 4535783, 40/40 requests returned both books but all 40 failed the complement-consistency check; the market identity then rolled to 4535990. On the new stable market, 20/20 fresh batches were complement-consistent. The stable-market fresh-book latency was p50 138 ms, p90 177.5 ms, p99 about 205.6 ms, max 209 ms. No economic fields were emitted in either sample.

This supports a rollover guard rather than a post-hoc TTE cutoff: when a fresh two-book batch is complement-incoherent, the challenger withdraws and takes no new risk. It resumes only on a fresh complement-consistent batch and records market-identity changes as diagnostics. No fixed TTE threshold is inferred from this development observation.

A transport-only A/B then isolated TLS setup as a material latency cost. Thirty one-shot urllib requests had p50 87.9 ms and p90 128.2 ms, while thirty requests over one persistent HTTP/1.1 connection had p50 45.7 ms, p90 57.5 ms and p99 91.5 ms, with 30/30 two-book responses in both arms. The shadow therefore uses HTTP/1.1 keep-alive and never retries a failed request inside the same scan tick.

A subsequent stable-market 80-tick keep-alive sample produced p50 44 ms, p90 57 ms and p99 81 ms. One 594 ms tail observation under the earlier 500 ms socket timeout motivated a purely mechanical contract: the book-request timeout now equals the 250 ms scan interval. A follow-up 80-tick transport sample under the 250 ms timeout had p50 41 ms, p90 48 ms, p99 83 ms and max 84 ms. That sample occurred in a complement-incoherent pre-rollover state, so it is latency evidence only and carries no economic claim.

## Safety and promotion

The challenger always has `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`, `real_capital_at_risk=false`, `execution_authority=ZERO_AUTHORITY_RESEARCH_ONLY`, and `automatic_promotion=false`. Any runtime integration, threshold change, sizing change or deployment requires a later exact-SHA review and a new prospective forward boundary.
