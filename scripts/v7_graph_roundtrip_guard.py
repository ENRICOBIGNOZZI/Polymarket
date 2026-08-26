#!/usr/bin/env python3
from __future__ import annotations

# Canonical runtime adapter for the v3 Graph/RV round-trip guard. The core is
# frozen separately so small runtime/import repairs do not risk rewriting the
# long statistical/economic implementation. This compatibility layer is removed
# in the post-main cleanup tranche.

from collections import Counter

import v7_graph_roundtrip_guard_core as core

core.Counter = Counter
_original_attach = core.attach_roundtrip_descriptor
_original_mature = core.mature_session
MAX_LIQUIDATION_LABEL_DELAY_MS = 45_000


def attach_roundtrip_descriptor(session, clob, window_seconds):
    """Use actual optimized target capital, not the intent's budget ceiling."""
    enriched, reason = _original_attach(session, clob, window_seconds)
    if enriched is None:
        return None, reason
    descriptor = enriched.get("execution_descriptor")
    if not isinstance(descriptor, dict):
        return None, "descriptor_missing_after_attach"
    descriptor_legs = {
        str(row.get("key") or ""): row
        for row in descriptor.get("legs", [])
        if isinstance(row, dict)
    }
    actual_notional = 0.0
    for leg in enriched.get("legs", []):
        if not isinstance(leg, dict):
            continue
        key = core._leg_key(leg)
        dleg = descriptor_legs.get(key)
        if dleg is None:
            return None, "descriptor_leg_mapping"
        target = max(0.0, float(core.v2.base.finite(leg.get("target_shares"), 0.0)))
        limit = float(core.v2.base.finite(leg.get("limit_price"), 0.0))
        fee = max(0.0, float(core.v2.base.finite(dleg.get("entry_fee_per_share"), 0.0)))
        actual_notional += target * (limit + fee)
    if actual_notional <= 1e-12:
        return None, "descriptor_zero_actual_target_notional"
    descriptor["quoted_max_notional"] = descriptor.get("max_notional")
    descriptor["max_notional"] = actual_notional
    enriched["max_notional"] = actual_notional
    enriched["actual_target_notional"] = actual_notional
    return enriched, reason


def mature_session(session, tape, gamma, clob, slippage_bps, capital_cost_bps_per_hour):
    """Reject sessions whose liquidation snapshot is no longer a 180s label."""
    deadline = int(session.get("deadline_received_ms") or 0)
    now = core.v2.base.now_ms()
    if deadline <= 0 or now < deadline:
        return None
    if now - deadline > MAX_LIQUIDATION_LABEL_DELAY_MS:
        return None
    outcome = _original_mature(
        session, tape, gamma, clob, slippage_bps, capital_cost_bps_per_hour
    )
    if outcome is None:
        return None
    observed = int(outcome.get("liquidation_book_received_ms") or now)
    delay = max(0, observed - deadline)
    if delay > MAX_LIQUIDATION_LABEL_DELAY_MS:
        return None
    outcome["liquidation_label_delay_ms"] = delay
    outcome["effective_liquidation_horizon_seconds"] = max(
        0.0,
        (observed - int(session.get("origin_received_ms") or observed)) / 1000.0,
    )
    return outcome


# Patch the core namespace used by core.main() so CLI and imported semantics are
# identical. Evidence returns are normalized by actual optimized target cash and
# only bounded-delay forward labels are admitted.
core.attach_roundtrip_descriptor = attach_roundtrip_descriptor
core.mature_session = mature_session

for _name in dir(core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(core, _name)
# Preserve patched wrappers after the re-export loop.
globals()["attach_roundtrip_descriptor"] = attach_roundtrip_descriptor
globals()["mature_session"] = mature_session


if __name__ == "__main__":
    raise SystemExit(core.main())
