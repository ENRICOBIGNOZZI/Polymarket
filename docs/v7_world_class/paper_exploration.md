# V7 PAPER research runtime

The current V7 research runtime is PAPER-only. It has one coordinator, one allocator/risk/OMS chain, one inventory owner and one canonical ledger writer. Authenticated execution and real-order submission remain disabled.

## Fair-value research model

BTC M5 uses one frozen `btc_m5_rich_external_logit_v1` research artifact. Training is explicit and offline; ordinary runtime startup performs inference only. A new artifact is created only when the research workflow deliberately retrains on a larger causal dataset.

The model consumes the causal Polymarket prior together with receive-time external information: multi-venue spot state, microprice, OFI, trade imbalance, short-horizon returns and volatility, derivative basis/funding/open interest, perp book state, Deribit volatility context and other available external features. Missing observations are represented explicitly rather than fabricated as zeros.

If the model uses Polymarket as a prior, its CLOB snapshot identity and receive timestamp are part of the causal feature cut. MAKE and TAKE revalidate prior age and repricing before acting; stale or already-absorbed information maps to `NOTHING`.

The structural same-oracle diffusion estimate remains a mathematical fallback when the frozen research artifact is unavailable.

## Maker execution learning

The Maker has one current-run research execution model. It starts from an explicit prior and is periodically refit from only the current PAPER ledger and current-run markout evidence. No archived execution evidence is imported.

The model estimates censored fill/survival behavior, queue and execution funnels, and fill-conditioned adverse markout. Its Beta-shrunk fill posterior informs PAPER economics after the declared minimum current-run evidence. There is one current research execution model and no parallel model-selection control plane.

## High-frequency external-information research

A zero-authority observer records causal rich feature cuts and Polymarket repricing at 100, 250, 500 and 1000 ms. This dataset supports an explicitly trained frozen lead/lag model for MAKE/CANCEL. The collector never trains, allocates capital or submits orders.

## Safety boundary

Research simplification does not remove technical invariants: `paper_only=true`, `real_order_submission=false`, one canonical execution/ledger chain, verified settlement semantics, receive-time causality, bounded exposure, and fail-closed behavior on malformed or stale state.
