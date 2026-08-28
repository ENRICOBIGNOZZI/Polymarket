#!/usr/bin/env python3
"""Build causal, trade-independent V7 settlement fair-value datasets.

Input forecasts are recorded for every eligible contract whether or not V7
trades. The output selects at most one observation per contract/TTE bucket and
joins terminal settlement evidence only after resolution. This avoids treating
high-frequency ticks as independent settlement labels and prevents trade-
selection bias from entering fair-value training.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
DEFAULT_TTE_BUCKETS = (240.0, 180.0, 120.0, 90.0, 60.0, 45.0, 30.0, 20.0, 15.0, 10.0, 5.0, 2.0)


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class ForecastObservation:
    contract_id: str
    market_handle: int
    rules_hash: str
    contract_version: int
    reference_version: int
    asset: str
    contract_start_ns: int
    contract_end_ns: int
    observation_monotonic_ns: int
    observation_wall_ns: int
    tte_seconds: float
    features: dict[str, float]
    feature_source_versions: dict[str, int]
    feature_receive_times_ns: dict[str, int]
    oracle_state_version: int
    external_state_version: int
    pm_state_version: int
    causal_cut_id: int
    max_input_receive_monotonic_ns: int
    pm_mid: float
    oracle_only_probability: float
    external_median_probability: float
    structural_probability: float
    calibrated_probability: float
    full_external_probability: float
    lower_probability: float
    upper_probability: float
    model_family: str
    model_version: str
    model_hash: str
    feature_schema_version: str

    def validate(self) -> None:
        if not self.contract_id or not self.asset or not self.rules_hash:
            raise DatasetError("forecast:identity_missing")
        if self.market_handle <= 0 or self.contract_version <= 0 or self.reference_version <= 0:
            raise DatasetError("forecast:version_missing")
        if self.contract_start_ns <= 0 or self.contract_end_ns <= self.contract_start_ns:
            raise DatasetError("forecast:contract_window_invalid")
        if self.observation_monotonic_ns <= 0 or self.observation_wall_ns <= 0:
            raise DatasetError("forecast:observation_clock_invalid")
        if self.max_input_receive_monotonic_ns <= 0 or self.max_input_receive_monotonic_ns > self.observation_monotonic_ns:
            raise DatasetError("forecast:causality_failure")
        if self.causal_cut_id <= 0:
            raise DatasetError("forecast:causal_cut_missing")
        if not math.isfinite(self.tte_seconds) or self.tte_seconds < 0.0:
            raise DatasetError("forecast:tte_invalid")
        if not isinstance(self.features, dict) or not isinstance(self.feature_source_versions, dict):
            raise DatasetError("forecast:features_invalid")
        if not isinstance(self.feature_receive_times_ns, dict) or not self.feature_receive_times_ns:
            raise DatasetError("forecast:feature_receive_times_missing")
        if any(int(value) > self.observation_monotonic_ns or int(value) <= 0
               for value in self.feature_receive_times_ns.values()):
            raise DatasetError("forecast:feature_receive_future")
        if self.oracle_state_version <= 0 or self.external_state_version <= 0 or self.pm_state_version <= 0:
            raise DatasetError("forecast:source_version_missing")
        probabilities = (
            self.pm_mid,
            self.oracle_only_probability,
            self.external_median_probability,
            self.structural_probability,
            self.calibrated_probability,
            self.full_external_probability,
            self.lower_probability,
            self.upper_probability,
        )
        if any(not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0 for value in probabilities):
            raise DatasetError("forecast:probability_invalid")
        if not self.lower_probability <= self.full_external_probability <= self.upper_probability:
            raise DatasetError("forecast:interval_invalid")
        if not self.model_family or not self.model_version or not self.model_hash or not self.feature_schema_version:
            raise DatasetError("forecast:model_identity_missing")


@dataclass(frozen=True)
class SettlementEvidence:
    contract_id: str
    market_handle: int
    rules_hash: str
    contract_version: int
    reference_version: int
    outcome: int
    settlement_source: str
    terminal_oracle_exact: str
    terminal_oracle_numeric: float
    opening_reference_exact: str
    opening_reference_numeric: float
    settlement_observation_wall_ns: int
    settlement_receive_monotonic_ns: int
    provenance_hash: str
    verified: bool

    def validate(self) -> None:
        if not self.contract_id or self.market_handle <= 0:
            raise DatasetError("settlement:identity_missing")
        if self.outcome not in (0, 1):
            raise DatasetError("settlement:outcome_not_binary")
        if not self.rules_hash or self.contract_version <= 0 or self.reference_version <= 0:
            raise DatasetError("settlement:version_missing")
        if not self.settlement_source or not self.terminal_oracle_exact or not self.opening_reference_exact:
            raise DatasetError("settlement:provenance_missing")
        if not math.isfinite(self.terminal_oracle_numeric) or not math.isfinite(self.opening_reference_numeric):
            raise DatasetError("settlement:value_invalid")
        if self.settlement_observation_wall_ns <= 0 or self.settlement_receive_monotonic_ns <= 0:
            raise DatasetError("settlement:clock_invalid")
        if not self.provenance_hash or not self.verified:
            raise DatasetError("settlement:not_verified")


@dataclass(frozen=True)
class DatasetRow:
    schema_version: int
    contract_id: str
    market_handle: int
    rules_hash: str
    contract_version: int
    reference_version: int
    asset: str
    observation_monotonic_ns: int
    observation_wall_ns: int
    tte_bucket_seconds: float
    observed_tte_seconds: float
    bucket_distance_seconds: float
    sample_weight: float
    features: dict[str, float]
    feature_source_versions: dict[str, int]
    feature_receive_times_ns: dict[str, int]
    oracle_state_version: int
    external_state_version: int
    pm_state_version: int
    causal_cut_id: int
    max_input_receive_monotonic_ns: int
    pm_mid: float
    oracle_only_probability: float
    external_median_probability: float
    structural_probability: float
    calibrated_probability: float
    full_external_probability: float
    lower_probability: float
    upper_probability: float
    model_family: str
    model_version: str
    model_hash: str
    feature_schema_version: str
    terminal_outcome: int
    settlement_source: str
    terminal_oracle_exact: str
    terminal_oracle_numeric: float
    opening_reference_exact: str
    opening_reference_numeric: float
    settlement_provenance_hash: str


def _load_jsonl(path: Path, cls: type[Any]) -> list[Any]:
    rows: list[Any] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = cls(**json.loads(line))
                row.validate()
            except Exception as exc:
                raise DatasetError(f"{path}:{line_no}:{exc}") from exc
            rows.append(row)
    return rows


def _settlements_by_contract(rows: Iterable[SettlementEvidence]) -> dict[str, SettlementEvidence]:
    result: dict[str, SettlementEvidence] = {}
    for row in rows:
        previous = result.get(row.contract_id)
        if previous is None:
            result[row.contract_id] = row
            continue
        if asdict(previous) != asdict(row):
            raise DatasetError(f"settlement:conflicting_evidence:{row.contract_id}")
    return result


def _compatible(forecast: ForecastObservation, settlement: SettlementEvidence) -> bool:
    return (
        forecast.market_handle == settlement.market_handle
        and forecast.rules_hash == settlement.rules_hash
        and forecast.contract_version == settlement.contract_version
        and forecast.reference_version == settlement.reference_version
    )


def build_dataset(
    forecasts: Iterable[ForecastObservation],
    settlements: Iterable[SettlementEvidence],
    *,
    tte_buckets: tuple[float, ...] = DEFAULT_TTE_BUCKETS,
    max_bucket_distance_seconds: float = 2.5,
) -> list[DatasetRow]:
    if not tte_buckets or any(not math.isfinite(x) or x < 0.0 for x in tte_buckets):
        raise DatasetError("buckets:invalid")
    if not math.isfinite(max_bucket_distance_seconds) or max_bucket_distance_seconds < 0.0:
        raise DatasetError("buckets:max_distance_invalid")

    settlement_map = _settlements_by_contract(settlements)
    grouped: dict[tuple[str, float], tuple[ForecastObservation, float]] = {}
    for forecast in forecasts:
        forecast.validate()
        settlement = settlement_map.get(forecast.contract_id)
        if settlement is None:
            continue
        if not _compatible(forecast, settlement):
            raise DatasetError(f"settlement:lineage_mismatch:{forecast.contract_id}")
        # A forecast observed after terminal settlement is never a valid sample.
        if forecast.observation_wall_ns >= settlement.settlement_observation_wall_ns:
            continue
        nearest = min(tte_buckets, key=lambda bucket: (abs(bucket - forecast.tte_seconds), -bucket))
        distance = abs(nearest - forecast.tte_seconds)
        if distance > max_bucket_distance_seconds:
            continue
        key = (forecast.contract_id, float(nearest))
        previous = grouped.get(key)
        # Deterministic nearest-to-bucket selection; receive time breaks exact ties.
        candidate_rank = (distance, forecast.observation_monotonic_ns)
        previous_rank = (
            (previous[1], previous[0].observation_monotonic_ns)
            if previous is not None else (math.inf, math.inf)
        )
        if candidate_rank < previous_rank:
            grouped[key] = (forecast, distance)

    per_contract_count: dict[str, int] = {}
    for contract_id, _ in grouped:
        per_contract_count[contract_id] = per_contract_count.get(contract_id, 0) + 1

    output: list[DatasetRow] = []
    for (contract_id, bucket), (forecast, distance) in sorted(
        grouped.items(), key=lambda item: (item[1][0].observation_wall_ns, item[0][0], -item[0][1])
    ):
        settlement = settlement_map[contract_id]
        count = per_contract_count[contract_id]
        output.append(DatasetRow(
            schema_version=SCHEMA_VERSION,
            contract_id=contract_id,
            market_handle=forecast.market_handle,
            rules_hash=forecast.rules_hash,
            contract_version=forecast.contract_version,
            reference_version=forecast.reference_version,
            asset=forecast.asset,
            observation_monotonic_ns=forecast.observation_monotonic_ns,
            observation_wall_ns=forecast.observation_wall_ns,
            tte_bucket_seconds=bucket,
            observed_tte_seconds=forecast.tte_seconds,
            bucket_distance_seconds=distance,
            sample_weight=1.0 / count,
            features=forecast.features,
            feature_source_versions=forecast.feature_source_versions,
            feature_receive_times_ns=forecast.feature_receive_times_ns,
            oracle_state_version=forecast.oracle_state_version,
            external_state_version=forecast.external_state_version,
            pm_state_version=forecast.pm_state_version,
            causal_cut_id=forecast.causal_cut_id,
            max_input_receive_monotonic_ns=forecast.max_input_receive_monotonic_ns,
            pm_mid=forecast.pm_mid,
            oracle_only_probability=forecast.oracle_only_probability,
            external_median_probability=forecast.external_median_probability,
            structural_probability=forecast.structural_probability,
            calibrated_probability=forecast.calibrated_probability,
            full_external_probability=forecast.full_external_probability,
            lower_probability=forecast.lower_probability,
            upper_probability=forecast.upper_probability,
            model_family=forecast.model_family,
            model_version=forecast.model_version,
            model_hash=forecast.model_hash,
            feature_schema_version=forecast.feature_schema_version,
            terminal_outcome=settlement.outcome,
            settlement_source=settlement.settlement_source,
            terminal_oracle_exact=settlement.terminal_oracle_exact,
            terminal_oracle_numeric=settlement.terminal_oracle_numeric,
            opening_reference_exact=settlement.opening_reference_exact,
            opening_reference_numeric=settlement.opening_reference_numeric,
            settlement_provenance_hash=settlement.provenance_hash,
        ))
    return output


def write_dataset(rows: Iterable[DatasetRow], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), sort_keys=True, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forecasts", type=Path, required=True)
    parser.add_argument("--settlements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-bucket-distance-seconds", type=float, default=2.5)
    args = parser.parse_args()
    forecasts = _load_jsonl(args.forecasts, ForecastObservation)
    settlements = _load_jsonl(args.settlements, SettlementEvidence)
    rows = build_dataset(
        forecasts,
        settlements,
        max_bucket_distance_seconds=args.max_bucket_distance_seconds,
    )
    write_dataset(rows, args.output)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "rows": len(rows),
        "independent_contracts": len({row.contract_id for row in rows}),
        "tte_buckets": sorted({row.tte_bucket_seconds for row in rows}, reverse=True),
        "total_sample_weight": sum(row.sample_weight for row in rows),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
