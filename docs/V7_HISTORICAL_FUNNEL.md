# Historical opportunity funnel

Archived attribution now includes all retained declared coordinator, rejection and authorization-publication streams, including compressed segments. Source prefix hashes, missing streams and generation-metadata provenance remain explicit. Decision attempts are deduplicated by observed generation, owner and decision timestamp; conflicting identities fail. Authorization attempt IDs are deduplicated independently. Retries with different identities remain attempts, not independent opportunities.

Legacy selected keys are attributed only when archived runtime metadata identifies the generation; unrecorded candidate inputs remain unknown. Archived Maker markout labels join by exact fill identity and never create cash. The compact historical summary and decision memo retain funnel counts, while the historical data-quality scorecard remains separate from current performance.

Read-only audit on 2026-09-09: 4,094 identified opportunities, 70,513 coordinator decisions, 30,922 authorization records, 39,348 legacy selected-only decisions, and 38 opportunities with observed markouts from 136 retained label sources. All 551 historical positions remain reconciled, with zero unexplained PnL. These are observed populations, not estimates of unrecorded opportunities. Auxiliary and markout inclusion does not prove every historical event-level quality exposure is observable.

Validation: 19 attribution tests, 7 decision-report tests, and all 144 CTest entries in Release, Debug and ASan/UBSan passed. Runtime deployment is intentionally deferred; live remains on 7b1abecd1379f02ab819a7a0b894185a0f47611e.
