# Causal-watermark audit — 15 September 2026

## Confirmed defect
The production delayed labeler requires both the consumed book watermark and the
published observer watermark to cover the exact target time. Its label grace is
75 ms, but the observer published its watermark every 1,000 ms. Thus even a
perfectly continuous event stream can be censored by publication scheduling.

`tests/test_v7_causal_watermark_cadence.py` replays the same continuous book stream
through the real BookTimeline and Labeler: 200 origins, 250 ms horizon, unchanged
75 ms grace. At 1,000 ms publication, 16/200 (8%) are observed; at 25 ms publication,
200/200 are observed. This is a controlled regression test, NOT a live coverage
estimate, NOT a profitability result, and not a claim that all live censoring
comes from watermark publication. Maker cohort resets and invalid L2 states remain
separate causes. Earlier 48/49 snapshot-availability checks did not establish
online labeler coverage and must not be used as an end-to-end improvement claim.

## Fix and safety
The dedicated fair-only observer publishes status at 25 ms; membership scans and
flow summaries stay at 1 Hz. The existing Maker observer stays unchanged. Status
publication follows queue drain and book flush. No price after the target can be
used as its price, no horizon/grace is widened, and no continuity rule is relaxed.

A second fix prevents new PAPER entries while post-fill accounting is awaiting
owner reconciliation. Maintenance failure remains fail-closed. The nominal 250 ms
router period is a scheduling target, not a hard end-to-end latency guarantee:
maintenance is still serialized in the same owner thread and may delay decisions.

## Validation and scope
The complete native Release CTest suite passed 173/173 after these changes.
The hot-path, selective challenger and selective protocol function tests now
actually run when invoked directly by CTest (5, 9 and 7 functions respectively).
All changes remain PAPER-only and introduce no authenticated or real-order path.
The selective challenger functions are research modules; their inclusion in the
repository does not itself wire them into live order authorization.

## Forward-window identity
The active aa5311a610980e944a09ee7a236e972ee3b364da manifest is
btc-m5-maker-forward-1789396755851-aa5311a61098, not the superseded 5b1be8 window.
Its interval is 14 September 16:39:15.851 to 15 September 00:39:15.851 CEST;
the 60-second grace ends at 00:40:15.851 CEST. No deploy or economic final look
is permitted before the applicable boundary.
