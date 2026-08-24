# Aggressive V5 live-paper promotion

Source research PR/branch/commit: #153 — `research/aggressive-v5-activity` @ `9b9754d7f00ddb8af8d74cc16ecab7f78ee69e51`.

This integration promotes the bounded challenger described in the source research PR:

- continuously run all five independent V5 paper sleeves instead of validating only their manifests;
- widen discovery to 500 markets per sleeve and lower discovery-only liquidity constraints;
- retain positive post-cost admission, fee/slippage/adverse-selection accounting, VWAP re-admission and the global 15% drawdown kill switch;
- treat candidate volume, tradable coverage and process freshness as observable runtime SLAs rather than evidence of alpha;
- keep authenticated execution and real-money trading disabled.

The PCA latent-factor lane is not an unconditional filter bypass. It requires available metadata, bounded hedge error, residual stability, a material residual displacement and positive maker-entry economics. External live-paper signals are limited to fresh direct probabilities with mapping and confidence controls; feature-only news and crypto observations remain research data.
