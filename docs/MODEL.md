# Model design

## 1. State variable

For outcome token `i`, let the executable market probability be `p_i,t`. The statistical layer works in log-odds:

\[
x_{i,t}=\log\frac{p_{i,t}}{1-p_{i,t}}.
\]

The conceptual decomposition is

\[
x_t=L_t+S_t+\varepsilon_t,
\]

where `L_t` is common low-rank structure, `S_t` is transient relative-value mispricing, and `epsilon_t` contains microstructure/noise.

Prices are never used without the executable book in the trading decision. Fair value is compared with the ask for long outcome-token trades.

## 2. Statistical engines

### PCA

On standardized log-odds changes,

\[
r_t=Bf_t+u_t,
\]

and the residual

\[
u_t=r_t-BB'r_t
\]

is the mean-reversion candidate.

### EW-PCA

The same factor model is estimated with exponentially decaying observation weights. This adapts loadings to regime changes rather than treating old and recent observations equally.

### Robust sparse PCA

The implementation alternates between a rank-`K` projection and soft-thresholding of the residual:

\[
X=L+S,
\qquad
L\approx P_K(X-S),
\qquad
S\leftarrow \mathcal S_\tau(X-L).
\]

The sparse component on the latest observation is treated as the candidate mispricing. This is a computational approximation to principal-component-pursuit logic, not an exact convex PCP solver.

### Hierarchical factors

Global EW factors are combined with robust category-local factors. A token therefore has both broad Polymarket exposure and local category exposure before a residual is called alpha.

### Graph residual

A sparse metadata graph is constructed from event identity, category and informative shared terms in market/event text. For node `i`, the short-horizon graph residual is

\[
g_{i,t}=\Delta x_{i,t}-
\frac{\sum_{j\in N(i)}w_{ij}\Delta x_{j,t}}
{\sum_{j\in N(i)}w_{ij}}.
\]

Only sufficiently similar neighbors enter. Same binary-market complementary tokens are not treated as same-sign neighbors. This is deliberately a relative-move model, not level smoothing of unrelated event probabilities.

## 3. Mean reversion and fair value

For a residual `s_i,t`, the default decay assumption is an OU-style half-life `H`:

\[
E_t[s_{i,t+h}]=s_{i,t}2^{-h/H}.
\]

Hence the expected reversion in log-odds is

\[
\Delta x^{rev}_{i,t}
=-s_{i,t}\left(1-2^{-h/H}\right),
\]

and

\[
q_{i,t}^{fair}=\sigma(x_{i,t}+\Delta x^{rev}_{i,t}).
\]

The engine currently keeps long-only outcome-token intents. An overvalued YES contract should generally appear as an undervalued NO token rather than requiring a synthetic short.

## 4. Logical / exact relative value

For binary complementary tokens, a simultaneous executable condition

\[
a^{YES}+a^{NO}+fees < 1
\]

creates a locked paper-arbitrage candidate.

For negative-risk multi-market events, the engine checks the sum of executable YES asks across the *discovered* outcome set. In v0.1 these observations are only **consistency candidates**: they are displayed but never allocated or counted in paper P&L until metadata proves that the event outcome set is complete. This guard prevents missing outcomes from manufacturing false arbitrage.

For a fully known binary YES/NO market, the paper broker pre-quotes both legs at one book snapshot and commits only if every requested leg is fully fillable after fees. This prevents partial-fill pseudo-arbitrage in the demo. Exact-basket exposure is additionally capped per event.

## 5. Net alpha

A statistical trade must satisfy

\[
\alpha^{net}
=
(q^{fair}-a)
-
fee
-
C_{micro}
-
\kappa U
>0,
\]

where `a` is the executable ask, `C_micro` contains a spread/slippage buffer, and `U` penalizes estimation uncertainty.

The score used for ranking is approximately

\[
score=\frac{\alpha^{net}}{U+spread}.
\]

## 6. Position sizing

The online allocator uses a capped quarter-Kelly proxy. For a token with price `p`, net edge `alpha`, and model uncertainty `U`,

\[
f_i^{raw}
=
\frac14
\frac{\alpha_i}
{p_i(1-p_i)+U_i^2}.
\]

This is subsequently clipped by token, event, per-trade, depth and gross-exposure limits.

The intent is not to claim that terminal Bernoulli variance is the exact short-horizon covariance model. It gives a conservative bounded denominator while live data accumulates; a full cross-token covariance optimizer can replace it without changing the alpha interfaces.

## 7. Drawdown control

Let

\[
D_t=1-\frac{V_t}{\max_{s\le t}V_s}.
\]

Actual risk is multiplied by `m(D_t)`, with full risk below 6%, progressive deleveraging from 6% to 12%, severe deleveraging above 12%, and an emergency flatten / zero-new-risk threshold at 13.5%. The 1.5 percentage-point buffer is intentional because the research target is a 15% maximum drawdown, while jumps and book gaps make an exact hard guarantee impossible.

The research objective is

\[
\max_\theta CAGR^{net}_{OOS}(\theta)
\quad\text{s.t.}\quad
MDD^{net}_{OOS}(\theta)\le 15\%,
\]

but the production interpretation must remain probabilistic: jumps can cross the threshold before the controller can trade.

## 8. Validation protocol

The engines should be evaluated separately and as an ensemble using walk-forward/live paper data. Required diagnostics include:

- net P&L and CAGR;
- maximum drawdown;
- turnover and fees;
- realized spread/slippage;
- hit rate and calibration by predicted edge bucket;
- P&L by engine/category/event;
- concentration and factor exposures;
- performance with taker-only versus maker-first execution assumptions;
- ablation of PCA, robust, hierarchical, graph and logical components.

Statistical positions are closed after a short minimum holding period once their live net alpha falls below the exit threshold; exact-arbitrage baskets are held unless the portfolio emergency controller requires liquidation.

The sophisticated model is accepted only if it beats the simple PCA/EW-PCA baselines out of sample after costs.
