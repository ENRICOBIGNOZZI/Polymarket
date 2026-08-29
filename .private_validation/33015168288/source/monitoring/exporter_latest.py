from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from exporter import Collector, ExporterHandler, Metrics, _float, _last_csv_row, _mtime, _read_json

_VERSION_RE = re.compile(r"paper_v(\d+(?:[._-]\d+)*)", re.IGNORECASE)
_RECORDER_FAILURE_PREFIXES = (
    "fatal: HTTP request failed:",
    "fatal: Gamma markets HTTP 503:",
)
_CANONICAL_RUNTIME_STATUS = "runtime_status.json"


def _version_tuple(text: str) -> tuple[int, ...]:
    match = _VERSION_RE.search(text)
    if not match:
        return ()
    return tuple(int(value) for value in re.findall(r"\d+", match.group(1)))


def _version_label(version: tuple[int, ...]) -> str:
    return "v" + ".".join(str(value) for value in version) if version else "unknown"


def detect_run_root(runs_base: Path, run_name: str) -> tuple[Path, tuple[int, ...]]:
    if run_name and run_name.lower() != "auto":
        root = runs_base / run_name
        return root, _version_tuple(run_name)
    candidates = [path for path in runs_base.iterdir() if path.is_dir() and _version_tuple(path.name)] if runs_base.exists() else []
    if not candidates:
        return runs_base / "paper_v4_live", (4,)
    candidates.sort(key=lambda path: (_version_tuple(path.name), path.stat().st_mtime_ns, path.name))
    root = candidates[-1]
    return root, _version_tuple(root.name)


def detect_config(config_dir: Path, version: tuple[int, ...], explicit: str | None) -> Path:
    """Return only the config belonging to the selected runtime version.

    A missing exact config is intentionally *not* replaced with a previous
    version's file. Silent config inheritance made a newly-created V7 run look
    like a V6 runtime in monitoring.
    """
    if explicit:
        return Path(explicit)
    if version:
        return config_dir / f"paper_v{version[0]}.json"
    configs = [path for path in config_dir.glob("paper_v*.json") if _version_tuple(path.name)]
    if configs:
        configs.sort(key=lambda path: (_version_tuple(path.name), path.name))
        return configs[-1]
    return config_dir / "paper_v4.json"


def collector_class(version: tuple[int, ...]):
    """Load only an exact-version enrichment adapter.

    The canonical runtime contract is version-neutral. Version adapters may
    enrich it, but a future V(N) must never silently inherit exporter_v(N-1).
    """
    major = version[0] if version else 0
    if major >= 4:
        try:
            module = importlib.import_module(f"exporter_v{major}")
        except ImportError:
            return Collector, "contract"
        cls = getattr(module, "COLLECTOR_CLASS", None) or getattr(module, f"V{major}Collector", None)
        if cls is not None:
            return cls, f"v{major}"
    return Collector, "contract" if major else "base"


def _recent_nonempty_lines(path: Path, limit: int = 20) -> list[str]:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return []
    return lines[-max(1, limit):]


def v6_runtime_data_health(root: Path, *, proxy_port: int | None = None) -> tuple[bool, str]:
    """V6-only data-path health check.

    This deliberately stays V6-specific. Future runtimes use their own exact
    adapter plus the canonical runtime contract and are never routed through
    this probe merely because their version number is greater than six.
    """
    if proxy_port is None:
        try:
            proxy_port = int(os.environ.get("V6_MARKET_PROXY_PORT", "9120"))
        except ValueError:
            return False, "invalid_v6_market_proxy_port"
    query = urllib.parse.urlencode(
        {"active": "true", "closed": "false", "limit": "1", "offset": "0", "liquidity_num_min": "0"}
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/markets?{query}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            if getattr(response, "status", 200) != 200:
                return False, "v6_market_proxy_http_failure"
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False, "v6_market_proxy_unhealthy"
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return False, "v6_market_proxy_empty"

    recorder_tail = _recent_nonempty_lines(root / "trade_recorder.log")
    if len(recorder_tail) >= 5:
        successes = sum(line.startswith("trade_recorder markets=") for line in recorder_tail)
        failures = sum(line.startswith(_RECORDER_FAILURE_PREFIXES) for line in recorder_tail)
        if successes == 0 and failures == len(recorder_tail):
            return False, "v6_recorder_data_path_unhealthy"
    return True, "ok"


def _contract_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    direct = root / _CANONICAL_RUNTIME_STATUS
    if direct.is_file():
        candidates.append(root)
    try:
        children = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        children = []
    for child in children:
        if (child / _CANONICAL_RUNTIME_STATUS).is_file():
            candidates.append(child)
    return candidates


def runtime_state_root(root: Path) -> Path | None:
    """Resolve a canonical status root without knowing a version's layout.

    Direct status wins. Otherwise exactly one immediate child may own the
    contract (V7 uses ``execution/``). Multiple child contracts are rejected as
    ambiguous instead of selecting one by version-specific convention.
    """
    direct = root / _CANONICAL_RUNTIME_STATUS
    if direct.is_file():
        return root
    candidates = [candidate for candidate in _contract_candidates(root) if candidate != root]
    return candidates[0] if len(candidates) == 1 else None


def runtime_contract_health(root: Path, version: tuple[int, ...]) -> tuple[bool, str]:
    state_root = runtime_state_root(root)
    if state_root is None:
        if version and version[0] >= 7:
            return False, "canonical_runtime_status_missing_or_ambiguous"
        return True, "ok"

    raw = _read_json(state_root / _CANONICAL_RUNTIME_STATUS)
    if not raw:
        return False, "canonical_runtime_status_invalid"

    expected = version[0] if version else None
    actual = raw.get("version")
    if expected is not None and actual is not None:
        try:
            if int(actual) != expected:
                return False, "canonical_runtime_version_mismatch"
        except (TypeError, ValueError):
            return False, "canonical_runtime_version_invalid"

    if raw.get("paper_only") is not True:
        return False, "canonical_runtime_not_paper"
    if raw.get("authenticated_execution") is True:
        return False, "canonical_runtime_authenticated_execution_enabled"

    required_numeric = ("equity", "pnl", "drawdown", "live_units", "gross_exposure")
    for key in required_numeric:
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False, f"canonical_runtime_{key}_invalid"
        if not (-float("inf") < value < float("inf")):
            return False, f"canonical_runtime_{key}_invalid"
    return True, "ok"


@dataclass
class CanonicalStatus:
    equity: float = 0.0
    pnl: float = 0.0
    drawdown: float = 0.0
    killed: float = 0.0
    live_units: float = 0.0
    reserved_cash: float = 0.0
    gross_exposure: float = 0.0
    realized_pnl: float = 0.0
    execution_imbalance: float = 0.0
    execution_staleness: float = 0.0
    oos_trades: float = 0.0
    oos_net_pnl: float = 0.0
    oos_stressed_net_pnl: float = 0.0
    oos_drawdown: float = 0.0
    oos_pvalue: float = 1.0
    oos_eligible: float = 0.0
    production_threshold: float = 0.0
    oos_staleness: float = 0.0


def _canonical_from_contract(root: Path, now: float) -> CanonicalStatus | None:
    state_root = runtime_state_root(root)
    if state_root is None:
        return None
    status_path = state_root / _CANONICAL_RUNTIME_STATUS
    raw = _read_json(status_path)
    if not raw:
        return None
    oos = raw.get("oos") if isinstance(raw.get("oos"), dict) else {}
    return CanonicalStatus(
        equity=_float(raw.get("equity")),
        pnl=_float(raw.get("pnl")),
        drawdown=_float(raw.get("drawdown")),
        killed=1.0 if bool(raw.get("killed")) else 0.0,
        live_units=_float(raw.get("live_units")),
        reserved_cash=_float(raw.get("reserved_cash")),
        gross_exposure=_float(raw.get("gross_exposure")),
        realized_pnl=_float(raw.get("realized_pnl")),
        execution_imbalance=_float(raw.get("execution_imbalance")),
        execution_staleness=_float(raw.get("execution_staleness")),
        oos_trades=_float(oos.get("trades")),
        oos_net_pnl=_float(oos.get("net_pnl")),
        oos_stressed_net_pnl=_float(oos.get("stressed_net_pnl")),
        oos_drawdown=_float(oos.get("max_drawdown")),
        oos_pvalue=_float(oos.get("bootstrap_pvalue"), 1.0),
        oos_eligible=1.0 if bool(oos.get("eligible_for_tiny_pilot")) else 0.0,
        production_threshold=_float(oos.get("production_threshold")),
        oos_staleness=max(0.0, now - (_mtime(status_path) or now)),
    )


def _canonical_fallback(root: Path, config: Path, now: float) -> CanonicalStatus:
    cfg = _read_json(config) or {}
    starting = _float(cfg.get("starting_capital"), 10000.0)
    eq_path = root / "multileg_equity.csv"
    row = _last_csv_row(eq_path) or {}
    equity = _float(row.get("equity"), starting)
    recorder_mtime = _mtime(root / "trade_tape.csv")
    broker_ts = _float(row.get("timestamp"), _mtime(eq_path) or now)
    component_ages = [max(0.0, now - broker_ts)]
    if recorder_mtime is not None:
        component_ages.append(max(0.0, now - recorder_mtime))

    imbalance = 0.0
    try:
        import csv
        by_bundle: dict[str, list[float]] = {}
        with (root / "multileg_legs.csv").open(newline="", encoding="utf-8") as handle:
            for leg in csv.DictReader(handle):
                target = max(0.0, _float(leg.get("target_shares")))
                filled = max(0.0, _float(leg.get("filled_shares")))
                fraction = min(1.0, max(0.0, filled / target)) if target > 1e-12 else 0.0
                by_bundle.setdefault(leg.get("bundle_id", ""), []).append(fraction)
        for fractions in by_bundle.values():
            if fractions:
                imbalance = max(imbalance, max(fractions) - min(fractions))
    except OSError:
        pass

    realized = 0.0
    try:
        import csv
        with (root / "bundle_ledger.csv").open(newline="", encoding="utf-8") as handle:
            realized = sum(_float(row.get("net_pnl")) for row in csv.DictReader(handle))
    except OSError:
        pass

    report_path = root / "walk_forward.json"
    report = _read_json(report_path) or {}
    oos = report.get("oos") if isinstance(report.get("oos"), dict) else {}
    stress = report.get("oos_cost_stress") if isinstance(report.get("oos_cost_stress"), dict) else {}
    return CanonicalStatus(
        equity=equity,
        pnl=equity - starting,
        drawdown=_float(row.get("drawdown")),
        killed=_float(row.get("killed")),
        live_units=_float(row.get("live_bundles")),
        reserved_cash=_float(row.get("reserved_cash")),
        gross_exposure=_float(row.get("gross_entry_cash")),
        realized_pnl=realized,
        execution_imbalance=imbalance,
        execution_staleness=max(component_ages) if component_ages else 0.0,
        oos_trades=_float(oos.get("trades")),
        oos_net_pnl=_float(oos.get("net_pnl")),
        oos_stressed_net_pnl=_float(stress.get("net_pnl")),
        oos_drawdown=_float(oos.get("max_drawdown")),
        oos_pvalue=_float(report.get("bootstrap_one_sided_pvalue"), 1.0),
        oos_eligible=1.0 if report.get("eligible_for_tiny_pilot") else 0.0,
        production_threshold=_float(report.get("production_threshold")),
        oos_staleness=max(0.0, now - (_mtime(report_path) or now)) if report else 0.0,
    )


class LatestCollector:
    def __init__(self, runs_base: Path, config_dir: Path, run_name: str, explicit_config: str | None, top_opportunities: int) -> None:
        self.runs_base = runs_base
        self.config_dir = config_dir
        self.run_name = run_name
        self.explicit_config = explicit_config
        self.top_opportunities = top_opportunities
        self._delegate_key: tuple[str, str, tuple[int, ...]] | None = None
        self._delegate = None
        self._adapter = "base"

    def _resolve(self):
        root, version = detect_run_root(self.runs_base, self.run_name)
        config = detect_config(self.config_dir, version, self.explicit_config)
        key = (str(root), str(config), version)
        if self._delegate is None or key != self._delegate_key:
            cls, adapter = collector_class(version)
            self._delegate = cls(root, config, self.top_opportunities)
            self._delegate_key = key
            self._adapter = adapter
        return root, version, config

    def health(self) -> tuple[bool, str]:
        root, version, _ = self._resolve()
        contract_ok, detail = runtime_contract_health(root, version)
        if not contract_ok:
            return contract_ok, detail
        if version and version[0] == 6:
            return v6_runtime_data_health(root)
        return True, "ok"

    def collect(self) -> str:
        root, version, config = self._resolve()
        now = time.time()
        adapter_error = 0
        try:
            detailed = self._delegate.collect()
        except Exception:
            detailed = ""
            adapter_error = 1

        status = _canonical_from_contract(root, now)
        contract_present = 1 if status is not None else 0
        if status is None:
            fallback_root = runtime_state_root(root) or root
            status = _canonical_fallback(fallback_root, config, now)

        metrics = Metrics()
        labels = {"version": _version_label(version), "run_root": root.name, "adapter": self._adapter}
        metrics.sample(
            "polymarket_runtime_info",
            1,
            help_text="Selected Polymarket runtime and exact monitoring adapter.",
            labels=labels,
        )
        metrics.sample(
            "polymarket_runtime_contract_present",
            contract_present,
            help_text="Whether the selected runtime publishes the canonical runtime_status.json contract.",
        )
        metrics.sample(
            "polymarket_runtime_adapter_scrape_error",
            adapter_error,
            help_text="Whether the optional exact-version enrichment adapter failed during this scrape.",
        )
        fields = {
            "polymarket_runtime_equity_usd": (status.equity, "Canonical latest-runtime marked equity."),
            "polymarket_runtime_pnl_usd": (status.pnl, "Canonical latest-runtime marked PnL."),
            "polymarket_runtime_drawdown_ratio": (status.drawdown, "Canonical latest-runtime drawdown ratio."),
            "polymarket_runtime_kill_switch": (status.killed, "Canonical latest-runtime kill-switch state."),
            "polymarket_runtime_live_units": (status.live_units, "Canonical latest-runtime live bundles/positions."),
            "polymarket_runtime_reserved_cash_usd": (status.reserved_cash, "Canonical latest-runtime reserved cash."),
            "polymarket_runtime_gross_exposure_usd": (status.gross_exposure, "Canonical latest-runtime gross execution exposure."),
            "polymarket_runtime_realized_pnl_usd_total": (status.realized_pnl, "Canonical latest-runtime cumulative realized paper PnL."),
            "polymarket_runtime_execution_imbalance_ratio": (status.execution_imbalance, "Canonical latest-runtime cross-leg execution imbalance."),
            "polymarket_runtime_execution_staleness_seconds": (status.execution_staleness, "Worst canonical execution-data staleness."),
            "polymarket_runtime_oos_trades": (status.oos_trades, "Canonical latest-runtime selected OOS trade count."),
            "polymarket_runtime_oos_net_pnl_usd": (status.oos_net_pnl, "Canonical latest-runtime OOS net PnL."),
            "polymarket_runtime_oos_stressed_net_pnl_usd": (status.oos_stressed_net_pnl, "Canonical latest-runtime stressed OOS net PnL."),
            "polymarket_runtime_oos_drawdown_ratio": (status.oos_drawdown, "Canonical latest-runtime OOS maximum drawdown."),
            "polymarket_runtime_oos_bootstrap_pvalue": (status.oos_pvalue, "Canonical latest-runtime OOS bootstrap p-value."),
            "polymarket_runtime_oos_eligible": (status.oos_eligible, "Canonical latest-runtime real-money escalation gate."),
            "polymarket_runtime_production_threshold": (status.production_threshold, "Canonical latest-runtime forward edge threshold."),
            "polymarket_runtime_oos_staleness_seconds": (status.oos_staleness, "Age of the canonical latest-runtime OOS report."),
        }
        for name, (value, help_text) in fields.items():
            metrics.sample(name, value, help_text=help_text)
        return detailed + metrics.render()


class LatestExporterHandler(ExporterHandler):
    collector: LatestCollector

    def do_GET(self) -> None:
        if self.path == "/healthz":
            try:
                healthy, detail = self.collector.health()
            except Exception:
                healthy, detail = False, "runtime_health_check_failed"
            body = (detail + "\n").encode("utf-8")
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def parse_latest_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-base", type=Path, default=Path("runs"))
    parser.add_argument("--run-name", default="auto", help="Versioned run directory name or 'auto'.")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--config", default=None, help="Optional explicit config path; otherwise selected by runtime version.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--top-opportunities", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_latest_args(argv)
    collector = LatestCollector(args.runs_base, args.config_dir, args.run_name, args.config, args.top_opportunities)
    LatestExporterHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), LatestExporterHandler)
    print(f"polymarket latest-version exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
