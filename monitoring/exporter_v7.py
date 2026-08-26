from __future__ import annotations

import csv
import time
from pathlib import Path

from exporter import Metrics, _float, _read_json

EXPORTER_V7_VERSION = "2.0.1"


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle) if row]
    except OSError:
        return []


class V7Collector:
    """Canonical V7-only monitoring collector."""

    def __init__(self, run_root: Path, config: Path, top_opportunities: int = 20) -> None:
        self.root = Path(run_root)
        self.execution = self.root / "execution"
        self.shadow = self.root / "shadow"
        self.config = Path(config)
        self.top_opportunities = top_opportunities

    def health(self) -> tuple[bool, str]:
        now = time.time()
        supervisor = _read_json(self.root / "v7_supervisor.json") or {}
        execution_supervisor = _read_json(self.execution / "v7_execution_supervisor.json") or {}
        runtime = _read_json(self.execution / "runtime_status.json") or {}
        proxy = _read_json(self.execution / "market_proxy_status.json") or {}
        if not supervisor or not supervisor.get("execution_alive"):
            return False, "v7_execution_not_alive"
        if not supervisor.get("shadow_alive"):
            return False, "v7_shadow_not_alive"
        if now - _float(supervisor.get("timestamp"), 0.0) > 60.0:
            return False, "v7_supervisor_stale"
        if not execution_supervisor or now - _float(execution_supervisor.get("timestamp"), 0.0) > 120.0:
            return False, "v7_execution_supervisor_stale"
        if runtime.get("schema") != "polymarket_v7_runtime_status_v1" or int(_float(runtime.get("version"))) != 7:
            return False, "v7_runtime_status_invalid"
        if runtime.get("paper_only") is not True or runtime.get("authenticated_execution") is not False:
            return False, "v7_runtime_not_paper_only"
        if proxy.get("schema") != "polymarket_v7_market_proxy_status_v1":
            return False, "v7_market_proxy_status_invalid"
        if now - _float(proxy.get("timestamp"), 0.0) > 180.0:
            return False, "v7_market_proxy_status_stale"
        recorder_tail = self._recent_lines(self.execution / "trade_recorder.log")
        if len(recorder_tail) >= 5:
            successes = sum(line.startswith("trade_recorder markets=") for line in recorder_tail)
            failures = sum(
                line.startswith("fatal: HTTP request failed:") or line.startswith("fatal: Gamma markets HTTP 503:")
                for line in recorder_tail
            )
            if successes == 0 and failures == len(recorder_tail):
                return False, "v7_recorder_data_path_unhealthy"
        return True, "ok"

    @staticmethod
    def _recent_lines(path: Path, limit: int = 20) -> list[str]:
        try:
            lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        except OSError:
            return []
        return lines[-limit:]

    def collect(self) -> str:
        metrics = Metrics()
        now = time.time()
        runtime = _read_json(self.execution / "runtime_status.json") or {}
        allocator = _read_json(self.execution / "allocator_status.json") or {}
        strategies = _rows(self.execution / "strategy_status.csv")
        supervisor = _read_json(self.root / "v7_supervisor.json") or {}

        metrics.sample(
            "polymarket_runtime_info",
            1,
            help_text="Canonical V7 PAPER runtime.",
            labels={"version": "v7", "run_root": self.root.name, "adapter": "v7"},
        )
        metrics.sample("polymarket_v7_runtime_info", 1, help_text="Unified V7 runtime active.")
        metrics.sample("polymarket_v7_execution_alive", 1 if supervisor.get("execution_alive") else 0, help_text="V7 execution child liveness.")
        metrics.sample("polymarket_v7_shadow_alive", 1 if supervisor.get("shadow_alive") else 0, help_text="V7 shadow scheduler liveness.")

        canonical = {
            "polymarket_runtime_equity_usd": (runtime.get("equity"), "Canonical V7 marked equity."),
            "polymarket_runtime_pnl_usd": (runtime.get("pnl"), "Canonical V7 marked PnL."),
            "polymarket_runtime_drawdown_ratio": (runtime.get("drawdown"), "Canonical V7 drawdown ratio."),
            "polymarket_runtime_kill_switch": (1 if runtime.get("killed") else 0, "Canonical V7 kill-switch state."),
            "polymarket_runtime_live_units": (runtime.get("live_units"), "Canonical V7 live units."),
            "polymarket_runtime_reserved_cash_usd": (runtime.get("reserved_cash"), "Canonical V7 reserved cash."),
            "polymarket_runtime_gross_exposure_usd": (runtime.get("gross_exposure"), "Canonical V7 gross exposure."),
            "polymarket_runtime_realized_pnl_usd_total": (runtime.get("realized_pnl"), "Canonical V7 cumulative realized PAPER PnL."),
            "polymarket_runtime_execution_imbalance_ratio": (runtime.get("execution_imbalance"), "Canonical V7 execution imbalance."),
            "polymarket_runtime_execution_staleness_seconds": (runtime.get("execution_staleness"), "Canonical V7 execution staleness."),
        }
        for name, (value, help_text) in canonical.items():
            metrics.sample(name, _float(value), help_text=help_text)

        metrics.sample("polymarket_allocator_state_present", 1 if allocator else 0, help_text="V7 allocator state present.")
        metrics.sample("polymarket_allocator_models_expected", _float(allocator.get("models_expected")), help_text="V7 expected strategy books.")
        metrics.sample("polymarket_allocator_models_alive", _float(allocator.get("models_alive")), help_text="V7 live strategy books.")
        metrics.sample("polymarket_allocator_global_gross_fraction", _float(allocator.get("global_gross_fraction")), help_text="V7 gross fraction.")

        for row in strategies:
            model = str(row.get("name") or row.get("expert") or "unknown")
            labels = {"model": model}
            metrics.sample("polymarket_model_info", 1, help_text="V7 strategy book present.", labels=labels)
            metrics.sample("polymarket_model_pnl_usd", _float(row.get("pnl")), help_text="V7 strategy PnL.", labels=labels)
            metrics.sample("polymarket_model_equity_usd", _float(row.get("equity")), help_text="V7 strategy equity.", labels=labels)
            metrics.sample("polymarket_model_open_positions", _float(row.get("open_positions")), help_text="V7 strategy open positions/live units.", labels=labels)
            metrics.sample("polymarket_model_fills_total", _float(row.get("fills")), help_text="V7 strategy PAPER fills.", labels=labels)
            metrics.sample("polymarket_model_alive", _float(row.get("alive")), help_text="V7 strategy liveness.", labels=labels)
            metrics.sample("polymarket_model_staleness_seconds", _float(row.get("status_age_seconds")), help_text="V7 strategy state age.", labels=labels)
            metrics.sample("polymarket_model_kill_switch", _float(row.get("killed")), help_text="V7 strategy local kill-switch state.", labels=labels)
            metrics.sample("polymarket_model_drawdown_ratio", _float(row.get("drawdown")), help_text="V7 strategy drawdown.", labels=labels)
            metrics.sample("polymarket_model_gross_exposure_usd", _float(row.get("gross_exposure")), help_text="V7 strategy gross exposure.", labels=labels)

        pca = _read_json(self.shadow / "pca_stat_arb.json") or {}
        metrics.sample("polymarket_v7_pca_bh_survivors", _float(pca.get("bh_survivors"), _float(pca.get("selected_hypotheses"))), help_text="V7 PCA statistically selected residual hypotheses.")
        metrics.sample("polymarket_v7_pca_shadow_candidates", _float(pca.get("shadow_candidates"), _float(pca.get("candidate_count"))), help_text="V7 PCA fixed-horizon shadow candidates.")
        metrics.sample("polymarket_v7_pca_promotion_ready", 1 if pca.get("promotion_ready") else 0, help_text="V7 PCA promotion gate state.")

        for label, filename in (("30m", "local_factor_30m.json"), ("60m", "local_factor_60m.json")):
            lf = _read_json(self.shadow / filename) or {}
            metrics.sample("polymarket_v7_local_factor_by_selected_pairs", _float(lf.get("by_selected_pairs")), help_text="V7 Local Factor BY-FDR selected pairs.", labels={"fidelity": label})
            metrics.sample("polymarket_v7_local_factor_signals", _float(lf.get("post_multiplicity_pair_signals")), help_text="V7 Local Factor post-multiplicity signals.", labels={"fidelity": label})
            metrics.sample("polymarket_v7_local_factor_promotion_ready", 1 if lf.get("promotion_ready") else 0, help_text="V7 Local Factor promotion gate.", labels={"fidelity": label})

        ranking = _read_json(self.shadow / "cross_sectional_rank.json") or {}
        for row in ranking.get("forward", []) if isinstance(ranking.get("forward"), list) else []:
            horizon = str(int(_float(row.get("horizon_minutes"))))
            labels = {"horizon_minutes": horizon}
            metrics.sample("polymarket_v7_rank_completed_sections", _float(row.get("completed_sections")), help_text="V7 ranking completed sections.", labels=labels)
            metrics.sample("polymarket_v7_rank_mean_ic", _float(row.get("mean_rank_ic")), help_text="V7 ranking mean rank IC.", labels=labels)
            metrics.sample("polymarket_v7_rank_tail_spread", _float(row.get("mean_top_bottom_logit_spread")), help_text="V7 ranking tail spread.", labels=labels)
            metrics.sample("polymarket_v7_rank_statistical_gate", 1 if row.get("forward_statistical_gate") else 0, help_text="V7 ranking statistical gate.", labels=labels)

        hf = _read_json(self.shadow / "hf_frequency_probe.json") or {}
        for row in hf.get("cadences", []) if isinstance(hf.get("cadences"), list) else []:
            cadence = str(int(_float(row.get("cadence_seconds"))))
            labels = {"cadence_seconds": cadence}
            metrics.sample("polymarket_v7_hf_nonempty_bucket_fraction", _float(row.get("nonempty_bucket_fraction")), help_text="V7 public-flow density by cadence.", labels=labels)
            metrics.sample("polymarket_v7_hf_maker_clearable_fraction", _float(row.get("maker_clearable_fraction")), help_text="V7 maker queues clearable by cadence.", labels=labels)
            metrics.sample("polymarket_v7_hf_max_clearance_ratio", _float(row.get("max_best_queue_clearance_ratio")), help_text="V7 maximum queue clearance ratio.", labels=labels)

        shadow_status = _read_json(self.shadow / "scheduler_status.json") or {}
        metrics.sample("polymarket_v7_shadow_staleness_seconds", max(0.0, now - _float(shadow_status.get("timestamp"), now)), help_text="Age of V7 shadow scheduler state.")
        return metrics.render()


COLLECTOR_CLASS = V7Collector
