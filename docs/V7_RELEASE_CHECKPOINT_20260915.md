# Verified release checkpoint — 15 September 2026

## Published and verified

Source commit: `5d30463c1ad1afc96e88dc26bde4c95e689409a2` (PR #941).
The complete native Release suite passed 173/173. GitHub run 34904243100
passed Release, Debug, ASan/UBSan and security audit. Monitoring run
34904243155 and single-writer run 34904243072 also passed.

This source commit includes the integrated work from #939/#940, the 25ms
fair-only watermark publication fix, and a fail-closed guard that prevents
new entries while post-fill account reconciliation is outstanding.
Previously uninvoked function tests now actually execute through CTest.

## What the measurements establish

Controlled replay through production BookTimeline/Labeler, same 200 origins,
same 250ms target, same 75ms grace:

| Watermark publication | Observed | Censored | Coverage |
|---|---:|---:|---:|
| 1000ms | 16 | 184 | 8% |
| 25ms | 200 | 0 | 100% |

A separate 90.04-second public-feed technical probe used the same 3063 origins
and the same consumed book stream for both arms. It reported no prices, PnL,
markouts, prediction accuracy or other economic endpoints:

| Published watermark arm | Observed | Censored | Coverage |
|---|---:|---:|---:|
| Current 25ms status | 2600 | 463 | 84.8841% |
| Same status downsampled to 1Hz | 155 | 2908 | 5.0604% |

There were 21 observer sessions. This is a short, correlated technical sample,
not 3063 independent economic observations and not a profitability claim.
The binary SHA256 was
`42267ba5834e6032e731e8eb680305333313351cdcf54b094b4c8ea13b45264d`.
Source runtime identity was `aa5311a610980e944a09ee7a236e972ee3b364da`.
Earlier 48/49 endpoint-availability checks did not establish online-labeler
coverage; Maker rotations cannot be asserted to be the sole or main cause
of the historical missing labels from those checks alone.

## Additional native correction already tested locally, not yet pushed

The integrated server worktree contains an uncommitted change to
`src/v7_maker_fillability_observer.cpp` and
`tests/test_v7_causal_book_observer.cpp`. The full native Release suite passed
173/173 after this change. Remote access then timed out and the connector
reported no available device, preventing its commit/push and deployment.
Do not discard the worktree or claim the correction is already in 5d30463c.

The correction clears a pending recovery request only when every instrument's
WS lineage has actually recovered and there are no decoder failures or dropped
events. It avoids restarting a stream already healed by a full WS snapshot.
A lost-frame/decoder failure remains latched; a later snapshot cannot erase
missing evidence. Existing decoder acceptance rules are unchanged.

Exact insertion at the end of `ExactWsObserver::on_frame`, after the queue loop:

```cpp
        // A later full WS snapshot may already have healed the affected token.
        // Do not restart a recovered stream merely because a past root failure
        // latched the request. Missing/damaged evidence can never self-heal.
        if (lineage_recovery_requested_.load(std::memory_order_acquire)
            && decoder_failures_.load(std::memory_order_relaxed) == 0
            && dropped_.load(std::memory_order_relaxed) == 0) {
            const bool still_missing = std::any_of(tokens_.begin(), tokens_.end(),
                [&](const SelectedToken& token) {
                    return decoder_->snapshot(token.instrument_handle).lineage_continuous == 0;
                });
            if (!still_missing) {
                lineage_recovery_requested_.store(false, std::memory_order_release);
                lineage_recovered_without_restart_.fetch_add(1, std::memory_order_relaxed);
            }
        }
```

Add member `std::atomic<std::uint64_t> lineage_recovered_without_restart_{0};`
next to `lineage_recovery_requests_`. Publish it in `write_status` as
`root["lineage_recovered_without_restart"] = lineage_recovered_without_restart_.load(std::memory_order_relaxed);`.

Exact test insertion before `observer.stop()` after the existing epoch-2 checks:

```cpp
        send(snapshot(1'700'000'001'600), 1600);
        assert(!observer.lineage_recovery_requested());
        assert(observer.lineage_recovery_requests() == 1);
        // An ordinary snapshot may restore lineage, never lost-frame evidence.
        send("{invalid-json", 1700);
        assert(observer.lineage_recovery_requested());
        send(snapshot(1'700'000'001'800), 1800);
        assert(observer.lineage_recovery_requested());
```

The attempted second public-feed probe after this correction returned a
transport timeout without a process receipt. Its execution/completion and
results are UNKNOWN. Inspect existing sessions/output before starting another.

## Remaining release and research blockers

- Last verified live checkout remained `aa5311a610980e944a09ee7a236e972ee3b364da`,
  PAPER-only, authentication and real orders disabled. Current live health
  after loss of remote access is unverified, not known to be stopped.
- The applicable frozen window is
  `btc-m5-maker-forward-1789396755851-aa5311a61098`, ending
  **15 September 2026 00:39:15.851 CEST**, plus 60s grace to 00:40:15.851.
  The 23:49 end belonged to the superseded 5b1be8 window.
- This session did not evaluate the frozen economic endpoint, deploy a successor,
  or start a new eight-hour experiment. Do not infer any of those operations
  from CI success or from these technical measurements.
- The selective decision functions are research modules, not yet connected to
  live order authorization. Their presence in the integrated tree is not proof
  that a selective strategy is running.
- Maintenance remains in the same owner thread. The 250ms period is nominal;
  slow maintenance or coordinator waits can still delay decisions. Measure the
  full signal-to-authorized-execution chain, not only HTTP round-trip time.
- Extend the permanent-evidence catalog for the dedicated
  `research/repricing_book/book_observations/*.jsonl*` and associated public
  trade stream. The catalog currently recognizes the old Maker paths only.
  Verify compression/retention and disk headroom before another full window.
- Physical separation of analytics was not performed. Serialization and
  resource-pressure deferral are not the same as moving analytics off-host.

## Safe continuation

Restore the existing authorized remote connection; inspect live health and
window identity first. Preserve the uncommitted native fix, reconcile this
document-only branch advance without force-push, rerun the complete test suite
and exact-head CI. Record the frozen final result without tuning its protocol.
Close the remaining wiring/retention/latency blockers before a controlled PAPER
cutover and a newly preregistered eight-hour experiment. Never alter or relabel
old evidence, bypass a failing gate, or enable real orders.
