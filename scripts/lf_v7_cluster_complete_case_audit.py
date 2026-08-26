#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class PanelResult:
    times: tuple[int, ...]
    markets: tuple[str, ...]


def longest_regular_suffix(times: Sequence[int], bucket_seconds: int) -> tuple[int, ...]:
    ordered = sorted(set(int(t) for t in times))
    if not ordered:
        return ()
    start = len(ordered) - 1
    while start > 0 and ordered[start] - ordered[start - 1] == bucket_seconds:
        start -= 1
    return tuple(ordered[start:])


def complete_case_panel(
    histories: Mapping[str, Mapping[int, float]],
    market_ids: Sequence[str],
    bucket_seconds: int,
    min_points: int,
) -> PanelResult | None:
    ids = tuple(mid for mid in market_ids if mid in histories)
    if not ids:
        return None
    common = set(histories[ids[0]])
    for mid in ids[1:]:
        common &= set(histories[mid])
    times = longest_regular_suffix(common, bucket_seconds)
    if len(times) < min_points:
        return None
    return PanelResult(times=times, markets=ids)


def predeclared_pair_panel(
    histories: Mapping[str, Mapping[int, float]],
    pair: tuple[str, str],
    controls: Sequence[str],
    bucket_seconds: int,
    min_points: int,
) -> PanelResult | None:
    # The control set is assumed to have been frozen from contract metadata before
    # price history is inspected.  This audit deliberately does not select controls
    # from realized returns, z-scores, p-values, edge, PnL or missingness.
    ids = (pair[0], pair[1], *tuple(controls))
    if len(set(ids)) != len(ids) or any(mid not in histories for mid in ids):
        return None
    return complete_case_panel(histories, ids, bucket_seconds, min_points)


def deterministic_fixture(points: int = 60, bucket_seconds: int = 3600) -> dict[str, dict[int, float]]:
    times = [i * bucket_seconds for i in range(points)]
    histories = {
        "A": {t: 0.1 + 0.001 * i for i, t in enumerate(times)},
        "B": {t: 0.2 - 0.001 * i for i, t in enumerate(times)},
        "C": {t: 0.3 + 0.002 * i for i, t in enumerate(times)},
        "D": {t: 0.4 - 0.002 * i for i, t in enumerate(times)},
    }
    # E is an unrelated member of the same broad cluster with only a short recent
    # suffix.  Adding E should not erase the estimability of the predeclared A-B
    # hypothesis whose controls were frozen as C,D.
    histories["E"] = {t: 0.5 + 0.003 * i for i, t in enumerate(times[-5:])}
    return histories


def audit() -> dict[str, object]:
    bucket = 3600
    min_points = 48
    histories = deterministic_fixture()
    baseline = complete_case_panel(histories, ["A", "B", "C", "D"], bucket, min_points)
    broadened = complete_case_panel(histories, ["A", "B", "C", "D", "E"], bucket, min_points)
    pair_local = predeclared_pair_panel(histories, ("A", "B"), ["C", "D"], bucket, min_points)
    return {
        "bucket_seconds": bucket,
        "minimum_regular_points": min_points,
        "baseline_cluster_points": len(baseline.times) if baseline else 0,
        "broadened_cluster_points": len(broadened.times) if broadened else 0,
        "pair_local_points": len(pair_local.times) if pair_local else 0,
        "baseline_estimable": baseline is not None,
        "broadened_cluster_estimable": broadened is not None,
        "predeclared_pair_estimable": pair_local is not None,
        "anti_monotone_universe_effect": bool(
            baseline is not None and broadened is None and pair_local is not None
        ),
    }


def main() -> int:
    print(json.dumps(audit(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
