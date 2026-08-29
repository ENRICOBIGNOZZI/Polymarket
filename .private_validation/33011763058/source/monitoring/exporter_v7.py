from __future__ import annotations

import time
from pathlib import Path

from exporter import Metrics, _float, _read_json
from exporter_v6 import V6Collector

EXPORTER_V7_VERSION = "1.0.0"


class V7Collector(V6Collector):
    """Reuse mature V6 execution metrics on V7/execution and add V7 shadow state."""

    def __init__(self, run_root: Path, config: Path, top_opportunities: int = 20) -> None:
        self.v7_root = Path(run_root)
        self.shadow_root = self.v7_root / "shadow"
        super().__init__(self.v7_root / "execution", config, top_opportunities)

    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        now = time.time()
        supervisor = _read_json(self.v7_root / "v7_supervisor.json") or {}
        metrics.sample("polymarket_v7_runtime_info", 1, help_text="Unified V7 runtime adapter active.")
        metrics.sample("polymarket_v7_execution_alive", 1 if supervisor.get("execution_alive") else 0, help_text="V7 execution child liveness.")
        metrics.sample("polymarket_v7_shadow_alive", 1 if supervisor.get("shadow_alive") else 0, help_text="V7 shadow scheduler liveness.")

        pca = _read_json(self.shadow_root / "pca_stat_arb.json") or {}
        metrics.sample("polymarket_v7_pca_bh_survivors", _float(pca.get("bh_survivors"), _float(pca.get("selected_hypotheses"))), help_text="V7 PCA statistically selected residual hypotheses.")
        metrics.sample("polymarket_v7_pca_shadow_candidates", _float(pca.get("shadow_candidates"), _float(pca.get("candidate_count"))), help_text="V7 PCA fixed-horizon shadow candidates.")
        metrics.sample("polymarket_v7_pca_promotion_ready", 1 if pca.get("promotion_ready") else 0, help_text="V7 PCA promotion gate state.")

        for label, filename in (("30m", "local_factor_30m.json"), ("60m", "local_factor_60m.json")):
            lf = _read_json(self.shadow_root / filename) or {}
            metrics.sample("polymarket_v7_local_factor_by_selected_pairs", _float(lf.get("by_selected_pairs")), help_text="V7 Local Factor BY-FDR selected pairs.", labels={"fidelity": label})
            metrics.sample("polymarket_v7_local_factor_signals", _float(lf.get("post_multiplicity_pair_signals")), help_text="V7 Local Factor post-multiplicity pair signals.", labels={"fidelity": label})
            metrics.sample("polymarket_v7_local_factor_promotion_ready", 1 if lf.get("promotion_ready") else 0, help_text="V7 Local Factor promotion gate state.", labels={"fidelity": label})

        ranking = _read_json(self.shadow_root / "cross_sectional_rank.json") or {}
        for row in ranking.get("forward", []) if isinstance(ranking.get("forward"), list) else []:
            horizon = str(int(_float(row.get("horizon_minutes"))))
            labels = {"horizon_minutes": horizon}
            metrics.sample("polymarket_v7_rank_completed_sections", _float(row.get("completed_sections")), help_text="Prospective ranking completed sections.", labels=labels)
            metrics.sample("polymarket_v7_rank_mean_ic", _float(row.get("mean_rank_ic")), help_text="Prospective ranking mean rank IC.", labels=labels)
            metrics.sample("polymarket_v7_rank_tail_spread", _float(row.get("mean_top_bottom_logit_spread")), help_text="Prospective top-minus-bottom relative-logit spread.", labels=labels)
            metrics.sample("polymarket_v7_rank_statistical_gate", 1 if row.get("forward_statistical_gate") else 0, help_text="Per-horizon prospective ranking statistical gate.", labels=labels)

        hf = _read_json(self.shadow_root / "hf_frequency_probe.json") or {}
        for row in hf.get("cadences", []) if isinstance(hf.get("cadences"), list) else []:
            cadence = str(int(_float(row.get("cadence_seconds"))))
            labels = {"cadence_seconds": cadence}
            metrics.sample("polymarket_v7_hf_nonempty_bucket_fraction", _float(row.get("nonempty_bucket_fraction")), help_text="Same-tape public-flow density by decision cadence.", labels=labels)
            metrics.sample("polymarket_v7_hf_maker_clearable_fraction", _float(row.get("maker_clearable_fraction")), help_text="Current maker queues clearable inside one cadence bucket.", labels=labels)
            metrics.sample("polymarket_v7_hf_max_clearance_ratio", _float(row.get("max_best_queue_clearance_ratio")), help_text="Maximum queue clearance ratio observed by cadence.", labels=labels)

        status = _read_json(self.shadow_root / "scheduler_status.json") or {}
        shadow_ts = _float(status.get("timestamp"), now)
        metrics.sample("polymarket_v7_shadow_staleness_seconds", max(0.0, now - shadow_ts), help_text="Age of V7 multi-frequency shadow scheduler state.")
        return base + metrics.render()


COLLECTOR_CLASS = V7Collector
