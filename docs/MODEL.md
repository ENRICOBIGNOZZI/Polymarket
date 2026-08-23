# Model design

## 1. State variable and clock

For outcome token `i`, let the market probability be `p_i,t`. The statistical layer works in log-odds:

\[
x_{i,t}=\log\frac{p_{i,t}}{1-p_{i,t}}.
\]

The execution loop may run every few seconds, but the factor model is sampled on a fixed one-minute clock. Historical CLOB observations keep their Unix timestamps and are aligned to the same grid. A missing observation is forward-filled only for a small bounded number of bars; stale series are removed from that factor fit. This avoids combining a five-second live return with a five-minute historical return as though they were the same object.

Conceptually,

\[
x_t=L_t+S_t+\varepsilon_t,
\]

where `L_t` is common structure, `S_t` is an idiosyncratic state and `epsilon_t` contains microstructure/noise.

## 2. Statistical engines

### PCA factor removal

PCA is estimated on standardized changes in log-odds,

\[
r_t=\Delta x_t,\qquad
z_{i,t}=\frac{r_{i,t}-\bar r_i}{\hat\sigma_i}.
\]

With the first `K` right singular vectors collected in `B`, the factor innovation and idiosyncratic innovation are

\[
\widehat z_t^{F}=z_tBB',
\qquad
\widehat e_t=z_t-\widehat z_t^{F}.
\]

Crucially, the engine **does not simply reverse the latest PCA residual return**. It converts residual innovations back to log-odds units and builds the cumulative idiosyncratic state

\[
S_{i,t}=\sum_{s\le t}\hat\sigma_i\widehat e_{i,s}.
\]

This is the object tested for mean reversion.

### EW-PCA

The factor space is estimated using exponentially decaying time weights. Recent observations therefore matter more when the cross-market covariance structure changes.

### Robust sparse PCA

For the robust engine the standardized return matrix is decomposed approximately as

\[
X=L+S,
\qquad
L\approx P_K(X-S),
\qquad
S\leftarrow \mathcal S_\tau(X-L),
\]

by alternating low-rank projection and soft thresholding. The sparse residual innovations are accumulated into a state and passed through the same mean-reversion test. This is a fast approximation to principal-component-pursuit logic, not an exact convex PCP solver.

### Hierarchical factors

Global EW factors are combined with robust category-local factors. A market can therefore share broad Polymarket shocks while still being judged relative to its own category.

### Graph residual

A sparse metadata graph is constructed from event identity, category and informative shared terms. For node `i`,

\[
g_{i,t}=\Delta x_{i,t}-
\frac{\sum_{j\in N(i)}w_{ij}\Delta x_{j,t}}
{\sum_{j\in N(i)}w_{ij}}.
\]

Only sufficiently similar neighbors enter, and complementary tokens from the same binary market are not treated as same-sign neighbors. This engine remains a short-horizon graph-relative-value model rather than a PCA residual-state model.

## 3. OU / AR(1) residual-state filter

For every PCA-family residual state, estimate

\[
S_{i,t+1}=a_i+\rho_i S_{i,t}+\eta_{i,t+1}.
\]

The implied long-run mean and half-life are

\[
\mu_i=\frac{a_i}{1-\rho_i},
\qquad
HL_i=-\frac{\log 2}{\log\rho_i}\,\Delta t.
\]

A residual is eligible only when:

- `0 < rho < 1` inside configured safety bounds;
- the implied half-life is finite and below the configured maximum;
- an approximate unit-root t statistic `(rho-1)/se(rho)` is sufficiently negative;
- the current state is sufficiently far from its stationary mean in standard-deviation units.

The current t statistic is a fast online screen, **not a claim of a fully corrected ADF test**. Thresholds must be chosen by walk-forward validation and can later be replaced by a richer stationarity test / false-discovery-control layer.

For horizon `h`,

\[
\mathbb E_t[S_{i,t+h}]
=\mu_i+\rho_i^h(S_{i,t}-\mu_i),
\]

so the expected change in log-odds attributable to convergence is

\[
\Delta x^{rev}_{i,t}(h)
=(\mu_i-S_{i,t})(1-\rho_i^h).
\]

The fair probability is therefore

\[
q_{i,t}^{fair}
=\sigma\!\left(x_{i,t}+\Delta x^{rev}_{i,t}(h)\right).
\]

The forecast innovation variance is propagated through the AR(1), then mapped from log-odds to probability space with the logistic derivative. This uncertainty enters the trading hurdle.

## 4. Logical / exact relative value

For binary complementary tokens, a simultaneous executable condition

\[
a^{YES}+a^{NO}+fees < 1
\]

creates a locked paper-arbitrage candidate.

For negative-risk multi-market events, the engine checks the sum of executable YES asks across the *discovered* outcome set. In v0.2 these observations are only **consistency candidates**: they are displayed but never allocated or counted in paper P&L until metadata proves that the event outcome set is complete. This prevents missing outcomes from manufacturing false arbitrage.

For a fully known binary YES/NO market, the paper broker pre-quotes both legs on one book snapshot and commits only if every requested leg is fully fillable after fees. Exact-basket exposure is capped per event.

## 5. Net alpha

A statistical trade must satisfy

\[
\alpha^{net}
=(q^{fair}-a)
-fee-C_{micro}-\kappa U>0,
\]

where `a` is the executable ask, `C_micro` contains a spread/slippage buffer and `U` is forecast/model uncertainty.

Ranking uses approximately

\[
score
=MRstrength\times
\frac{\alpha^{net}}{U+spread},
\]

where stronger evidence against a unit root increases `MRstrength` subject to caps.

## 6. Position sizing

The online allocator uses a capped quarter-Kelly proxy. For price `p`, net edge `alpha` and model uncertainty `U`,

\[
f_i^{raw}
=\frac14\frac{\alpha_i}{p_i(1-p_i)+U_i^2}.
\]

It is subsequently clipped by token, event, per-trade, displayed-depth and gross-exposure limits. Multiple model signals on the same token do not create multiple independent bets: only the strongest signal sizes the token.

This is intentionally a robust online proxy. A full cross-token covariance / CDaR optimizer is a planned replacement once enough forward paper data exists to estimate its inputs without inventing precision.

## 7. Exits and drawdown control

Statistical positions are closed against displayed bids after a minimum holding period once their live net alpha falls below the exit threshold. Exact binary-arbitrage baskets are normally held, but can be liquidated by the portfolio emergency controller.

Let

\[
D_t=1-\frac{V_t}{\max_{s\le t}V_s}.
\]

Actual risk is multiplied by `m(D_t)`: full risk below 6%, progressive deleveraging from 6% to 12%, severe deleveraging above 12%, and emergency flatten / zero new risk at 13.5%. The 1.5 percentage-point buffer is intentional because the research target is a 15% maximum drawdown while jumps and book gaps make an exact hard guarantee impossible.

The research objective is

\[
\max_\theta CAGR^{net}_{OOS}(\theta)
\quad\text{s.t.}\quad
MDD^{net}_{OOS}(\theta)\le15\%.
\]

## 8. Validation protocol

All engines are evaluated separately and as an ensemble using walk-forward and live paper data. Required diagnostics include:

- net P&L and CAGR;
- maximum drawdown and drawdown duration;
- turnover and fees;
- realized spread/slippage;
- edge calibration by forecast bucket;
- P&L by engine/category/event;
- concentration and factor exposures;
- residual half-life and unit-root-screen diagnostics;
- maker-only / maker-first / taker-only execution sensitivity;
- ablation of PCA, EW-PCA, robust, hierarchical, graph and logical components.

The sophisticated model is accepted only if it beats the simple PCA/EW-PCA baselines out of sample after costs.
