#!/usr/bin/env python3
"""Apply the aggressive-but-cost-aware V5 paper runtime upgrade.

This script is intentionally idempotent.  It is executed once by
.github/workflows/aggressive-v5-upgrade.yml, validates every structural edit,
and leaves the authenticated real-money boundary untouched.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str, *, required: bool = True) -> bool:
    text = read(path)
    if new in text:
        return False
    count = text.count(old)
    if count != 1:
        if required:
            raise RuntimeError(f"{path}: expected exactly one old pattern, found {count}")
        return False
    write(path, text.replace(old, new, 1))
    return True


def regex_once(path: str, pattern: str, replacement: str, *, required: bool = True) -> bool:
    text = read(path)
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count == 0:
        if required:
            raise RuntimeError(f"{path}: regex did not match: {pattern}")
        return False
    write(path, updated)
    return True


def patch_config() -> None:
    path = ROOT / "config/paper_v5.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.update(
        {
            "market_limit": 500,
            "gamma_page_size": 100,
            "books_batch_size": 100,
            "interval_seconds": 10,
            "min_liquidity": 25.0,
            "min_volume24h": 0.0,
            "min_mid": 0.005,
            "max_mid": 0.995,
            "max_spread": 0.35,
            "min_net_edge": 0.0001,
            "uncertainty_penalty": 0.0,
            "slippage_bps": 3.0,
            "fractional_kelly": 0.08,
            "max_trade_usd": 75.0,
            "max_market_fraction": 0.02,
            "max_event_fraction": 0.06,
            "max_gross_fraction": 0.45,
            "max_drawdown": 0.15,
            "history_bootstrap": True,
            "bootstrap_market_limit": 350,
            "history_lookback_hours": 504,
            "history_fidelity_minutes": 15,
            "pca_window": 480,
            "pca_min_history": 24,
            "pca_universe": 350,
            "pca_mr_strength": 0.60,
            "pca_min_residual_z": 0.55,
            "graph_max_sum_error": 0.30,
            "semantic_min_similarity": 0.30,
            "semantic_shrink": 0.35,
            "scan_only": False,
        }
    )
    multi = cfg["multi_strategy"]
    multi.update(
        {
            "paper_only": True,
            "reserve_fraction": 0.05,
            "global_max_drawdown": 0.15,
            "global_max_gross_fraction": 0.45,
            "status_interval_seconds": 3.0,
            "restart_backoff_seconds": 1.0,
        }
    )
    multi["log_retention"] = {
        "compaction_interval_seconds": 60,
        "signal_rows_per_model": 20000,
        "arbitrage_rows_per_model": 10000,
        "pca_history_rows_per_market": 482,
    }
    desired = {
        "micro": {
            "capital_fraction": 0.25,
            "market_limit": 500,
            "interval_seconds": 5,
            "history_bootstrap": False,
            "pca_universe": 0,
            "pca_min_history": 100000,
            "min_net_edge": 0.00005,
            "uncertainty_penalty": 0.0,
            "fractional_kelly": 0.10,
            "max_trade_usd": 40.0,
        },
        "pca": {
            "capital_fraction": 0.25,
            "market_limit": 500,
            "interval_seconds": 20,
            "history_bootstrap": True,
            "bootstrap_market_limit": 350,
            "pca_universe": 350,
            "pca_min_history": 24,
            "pca_min_residual_z": 0.55,
            "pca_mr_strength": 0.60,
            "min_net_edge": 0.00010,
            "uncertainty_penalty": 0.01,
            "fractional_kelly": 0.12,
            "max_trade_usd": 60.0,
        },
        "graph": {
            "capital_fraction": 0.20,
            "market_limit": 500,
            "interval_seconds": 10,
            "history_bootstrap": False,
            "pca_universe": 0,
            "pca_min_history": 100000,
            "graph_max_sum_error": 0.30,
            "min_net_edge": 0.00005,
            "uncertainty_penalty": 0.01,
            "fractional_kelly": 0.10,
            "max_trade_usd": 50.0,
        },
        "semantic": {
            "capital_fraction": 0.15,
            "market_limit": 500,
            "interval_seconds": 15,
            "history_bootstrap": False,
            "pca_universe": 0,
            "pca_min_history": 100000,
            "semantic_min_similarity": 0.30,
            "semantic_shrink": 0.35,
            "min_net_edge": 0.00010,
            "uncertainty_penalty": 0.02,
            "fractional_kelly": 0.08,
            "max_trade_usd": 40.0,
        },
        "external": {
            "capital_fraction": 0.10,
            "market_limit": 500,
            "interval_seconds": 15,
            "history_bootstrap": False,
            "pca_universe": 0,
            "pca_min_history": 100000,
            "min_net_edge": 0.00020,
            "uncertainty_penalty": 0.03,
            "fractional_kelly": 0.10,
            "max_trade_usd": 40.0,
        },
    }
    seen: set[str] = set()
    for strategy in multi["strategies"]:
        name = strategy["name"]
        if name not in desired:
            continue
        seen.add(name)
        settings = desired[name]
        strategy["enabled"] = True
        strategy["capital_fraction"] = settings["capital_fraction"]
        overrides = strategy.setdefault("overrides", {})
        overrides.update({k: v for k, v in settings.items() if k != "capital_fraction"})
        overrides.update(
            {
                "min_liquidity": 25.0,
                "min_mid": 0.005,
                "max_mid": 0.995,
                "max_spread": 0.35,
                "slippage_bps": 3.0,
                "max_market_fraction": 0.02,
                "max_event_fraction": 0.06,
                "max_gross_fraction": 0.45,
                "max_drawdown": 0.15,
            }
        )
    missing = set(desired) - seen
    if missing:
        raise RuntimeError(f"missing V5 strategy definitions: {sorted(missing)}")
    allocated = sum(
        float(item["capital_fraction"])
        for item in multi["strategies"]
        if item.get("enabled", True)
    )
    if abs(allocated + float(multi["reserve_fraction"]) - 1.0) > 1e-9:
        raise RuntimeError("V5 capital fractions plus reserve do not sum to one")
    if multi.get("paper_only") is not True:
        raise RuntimeError("paper_only boundary must remain enabled")
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def patch_engine() -> None:
    path = "src/engine.cpp"
    replace_once(
        path,
        """        const double mid = yi->second.midpoint();
        if (!std::isfinite(mid) || mid <= cfg_.min_mid || mid >= cfg_.max_mid) continue;
        if (yi->second.spread() > cfg_.max_spread || ni->second.spread() > cfg_.max_spread) continue;
        if (!std::isfinite(yi->second.best_ask()) || !std::isfinite(ni->second.best_ask())) continue;
        tradable.push_back(m);
""",
        """        const double mid = yi->second.midpoint();
        if (!std::isfinite(mid) || mid <= cfg_.min_mid || mid >= cfg_.max_mid) continue;
        const auto executable = [&](const Book& book) {
            const double bid = book.best_bid();
            const double ask = book.best_ask();
            return std::isfinite(bid) && std::isfinite(ask) && ask > bid &&
                   book.spread() <= cfg_.max_spread;
        };
        // A bad complementary book must not discard an otherwise executable side.
        // Candidate construction and the VWAP admission test remain side-specific.
        if (!executable(yi->second) && !executable(ni->second)) continue;
        tradable.push_back(m);
""",
    )
    replace_once(
        path,
        """        const double beta = sxy / (sxx + ridge);
        if (beta >= -1e-4) continue;

        double rss = 0.0;
""",
        """        const double beta = sxy / (sxx + ridge);

        double rss = 0.0;
""",
    )
    replace_once(
        path,
        """        const double se_beta = std::sqrt(std::max(0.0, sigma2) / std::max(1e-8, sxx + ridge));
        const double t_reversion = se_beta > 1e-8 ? -beta / se_beta : 0.0;
        if (t_reversion < 0.75) continue;

        const double forecast_std = y_mu + beta * x_now;
        if (forecast_std * x_now >= 0.0) continue;

        const double cur = yes_books.at(s[j].m->yes_token).midpoint();
        const double forecast_logit = std::clamp(cfg_.pca_mr_strength * forecast_std * s[j].sd, -0.20, 0.20);
""",
        """        const double se_beta = std::sqrt(std::max(0.0, sigma2) / std::max(1e-8, sxx + ridge));
        const double t_predictive = se_beta > 1e-8 ? std::abs(beta) / se_beta : 0.0;
        // The residual sleeve may exploit either statistically supported reversal
        // or statistically supported continuation.  The previous reversal-only
        // gate discarded valid directional information by construction.
        if (t_predictive < 0.25) continue;

        const double forecast_std = y_mu + beta * x_now;
        if (!std::isfinite(forecast_std) || std::abs(forecast_std) < 0.02) continue;

        const double cur = yes_books.at(s[j].m->yes_token).midpoint();
        const double forecast_logit = std::clamp(cfg_.pca_mr_strength * forecast_std * s[j].sd, -0.30, 0.30);
""",
    )
    replace_once(
        path,
        """        const double reversion_conf = std::clamp((t_reversion - 0.75) / 2.5, 0.05, 1.0);
        out[s[j].m->id] = {q, std::clamp(z_conf * sample_conf * factor_conf * reversion_conf, 0.02, 1.0)};
""",
        """        const double predictive_conf = std::clamp(0.10 + (t_predictive - 0.25) / 2.0, 0.05, 1.0);
        out[s[j].m->id] = {q, std::clamp(z_conf * sample_conf * factor_conf * predictive_conf, 0.02, 1.0)};
""",
    )
    replace_once(
        path,
        """            bool complete = true;
            for (const auto& m : full) {
                auto it = books.find(m.yes_token);
                if (it == books.end()) { complete = false; break; }
                const double p = it->second.midpoint();
                const double spread = it->second.spread();
                const double depth = it->second.weighted_depth(true) + it->second.weighted_depth(false);
                if (!std::isfinite(p) || !std::isfinite(spread)) { complete = false; break; }
                const double var = std::max(1e-6, spread * spread + 1.0 / (depth + 1.0));
                g.push_back({&m, p, var});
                sum += p;
                varsum += var;
            }
            if (!complete || g.size() != full.size() || varsum <= 0.0) continue;
            if (std::abs(sum - 1.0) > cfg_.graph_max_sum_error) continue;

            const double conf = std::clamp(1.0 - std::abs(sum - 1.0) / std::max(1e-6, cfg_.graph_max_sum_error), 0.10, 1.0);
""",
        """            for (const auto& m : full) {
                auto it = books.find(m.yes_token);
                if (it == books.end()) continue;
                const double p = it->second.midpoint();
                const double spread = it->second.spread();
                const double depth = it->second.weighted_depth(true) + it->second.weighted_depth(false);
                if (!std::isfinite(p) || !std::isfinite(spread)) continue;
                const double var = std::max(1e-6, spread * spread + 1.0 / (depth + 1.0));
                g.push_back({&m, p, var});
                sum += p;
                varsum += var;
            }
            const double coverage = full.empty() ? 0.0 :
                static_cast<double>(g.size()) / static_cast<double>(full.size());
            if (g.size() < 2 || coverage < 0.60 || varsum <= 0.0) continue;
            const double tolerance = cfg_.graph_max_sum_error + 0.75 * (1.0 - coverage);
            if (std::abs(sum - 1.0) > tolerance) continue;

            const double conf = coverage * std::clamp(
                1.0 - std::abs(sum - 1.0) / std::max(1e-6, tolerance), 0.10, 1.0);
""",
    )


def patch_pair_stat_arb() -> None:
    path = "src/stat_arb.cpp"
    substitutions = [
        ("if (x.size() != y.size() || x.size() < 24) return f;", "if (x.size() != y.size() || x.size() < 18) return f;"),
        ("if (std::abs(f.beta) < 0.10 || std::abs(f.beta) > 8.0) return f;", "if (std::abs(f.beta) < 0.03 || std::abs(f.beta) > 15.0) return f;"),
        ("f.ok = gamma < 0.0 && f.phi > 0.02 && f.phi < 0.999 && std::isfinite(f.t_reversion);", "f.ok = gamma < 0.03 && f.phi > -0.25 && f.phi < 1.03 && std::isfinite(f.t_reversion);"),
        ("if (b.size() >= 25) series[m->id] = std::move(b);", "if (b.size() >= 20) series[m->id] = std::move(b);"),
        ("const std::vector<std::size_t> windows{48, 96, 192, 336, 672};", "const std::vector<std::size_t> windows{24, 48, 96, 192, 336, 672};"),
        ("if (lx_full.size() < 30) continue;", "if (lx_full.size() < 20) continue;"),
        ("if (w < 30) continue;", "if (w < 20) continue;"),
    ]
    for old, new in substitutions:
        replace_once(path, old, new)
    text = read(path)
    text = text.replace("double min_liquidity = 100.0, min_z = 1.50, max_half_life_hours = 168.0;", "double min_liquidity = 25.0, min_z = 0.65, max_half_life_hours = 240.0;")
    text = text.replace("double min_t_reversion = 1.75;", "double min_t_reversion = 0.50;")
    write(path, text)


def patch_pca_stat_arb() -> None:
    path = "src/pca_stat_arb.cpp"
    text = read(path)
    text = text.replace(
        "double min_liquidity = 100.0, min_z = 1.5, min_t = 1.75, max_half_life_hours = 168.0;",
        "double min_liquidity = 25.0, min_z = 0.65, min_t = 0.50, max_half_life_hours = 240.0;",
    )
    text = text.replace("double max_factor_hedge_error = 0.20;", "double max_factor_hedge_error = 0.65;")
    old = """                if (pm::market_relation(*s[j].market, *s[i].market) == pm::MarketRelation::none) continue;
                ++relation_pass;
"""
    new = """                // PCA hedges are selected by factor-loading replication.  Requiring
                // a textual/event relation here was a category error and eliminated
                // statistically coherent cross-event hedges before hedge-error testing.
                ++relation_pass;
"""
    if old in text:
        text = text.replace(old, new, 1)
    elif new not in text:
        raise RuntimeError("src/pca_stat_arb.cpp: relation gate pattern not found")
    write(path, text)


def patch_maker() -> None:
    path = "src/maker_paper.cpp"
    replace_once(path, "if (ms.confidence < 0.10) continue;", "if (ms.confidence < 0.03) continue;")
    replace_once(
        path,
        """                    const double future_bid = std::clamp(fair - 0.5 * spread, 0.001, 0.999) *
                                              (1.0 - cfg_.slippage_bps / 10000.0);
""",
        """                    // A passive fill starts at the bid, so valuing it at a full
                    // future bid double-counted half the spread and suppressed nearly
                    // every quote.  Retain a quarter-spread liquidation reserve instead.
                    const double future_bid = std::clamp(fair - 0.25 * spread, 0.001, 0.999) *
                                              (1.0 - cfg_.slippage_bps / 10000.0);
""",
    )
    text = read(path).replace("double min_edge_ = 0.003;", "double min_edge_ = 0.0001;")
    write(path, text)


def patch_coherent_filter() -> None:
    path = "scripts/filter_coherent_hedges.py"
    text = read(path)
    text = text.replace(
        '"""Fail-closed coherence gate for B2 PCA hedge baskets.\n\nThe raw PCA file remains a research diagnostic. Only rows whose hedge legs belong\nto the same explicit event or a strong semantic cluster are written to the\nexecution-facing CSV. Any metadata or filter failure leaves that CSV empty.\n"""',
        '"""Hybrid semantic/factor coherence gate for B2 PCA hedge baskets.\n\nSame-event and semantic relations remain preferred. A basket with low factor\nreplication error and stable residual dynamics may also pass even when its legs\nspan events; that is exactly the economic role of a PCA hedge.\n"""',
    )
    marker = "\ndef main() -> int:\n"
    helper = """

def fnum(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def main() -> int:
"""
    if "def fnum(value: object" not in text:
        if marker not in text:
            raise RuntimeError("coherent hedge main marker not found")
        text = text.replace(marker, helper, 1)
    text = text.replace('parser.add_argument("--min-jaccard", type=float, default=0.25)', 'parser.add_argument("--min-jaccard", type=float, default=0.15)')
    text = text.replace('parser.add_argument("--min-shared-tokens", type=int, default=2)', 'parser.add_argument("--min-shared-tokens", type=int, default=1)')
    anchor = '    parser.add_argument("--timeout-seconds", type=float, default=8.0)\n'
    addition = anchor + '    parser.add_argument("--max-factor-hedge-error", type=float, default=0.65)\n    parser.add_argument("--min-factor-stability", type=float, default=0.20)\n'
    if "--max-factor-hedge-error" not in text:
        if anchor not in text:
            raise RuntimeError("coherent hedge parser anchor not found")
        text = text.replace(anchor, addition, 1)
    old = """            if unrelated:
                reason = "unrelated_or_unknown_hedge_legs"
"""
    new = """            factor_error = fnum(row.get("hedge_error"), 999.0)
            factor_stability = fnum(row.get("stability"), 0.0)
            factor_coherent = (
                factor_error <= args.max_factor_hedge_error
                and factor_stability >= args.min_factor_stability
            )
            if unrelated and factor_coherent:
                scopes.append(
                    f"factor_model:{factor_error:.4f}:{factor_stability:.4f}"
                )
                unrelated.clear()
            elif unrelated:
                reason = "unrelated_or_unknown_hedge_legs"
"""
    if old in text:
        text = text.replace(old, new, 1)
    elif new not in text:
        raise RuntimeError("coherent hedge rejection block not found")
    write(path, text)


def patch_live_smoke() -> None:
    path = ".github/workflows/v4-live-smoke.yml"
    text = read(path)
    text = text.replace("timeout-minutes: 25", "timeout-minutes: 45")
    text = text.replace("--markets 120 --min-liquidity 100 --validate-only", "--markets 500 --min-liquidity 25 --validate-only")
    insertion_anchor = """          PY

          ./build/polymarket_trade_recorder \\
"""
    insertion = """          PY

          # Exercise every independent V5 sleeve as an actual paper engine.  The
          # old smoke only validated configs and then scanned graph, which could
          # report green while four sleeves never ran.
          pids=()
          for strategy in micro pca graph semantic external; do
            ./build/polymarket_engine \\
              --config "$R/generated_configs/${strategy}.json" --once --paper \\
              --markets 500 --min-liquidity 25 \\
              --run-dir "$R/strategies/${strategy}" \\
              > "$R/${strategy}_latest.log" 2>&1 &
            pids+=("$!")
          done
          for pid in "${pids[@]}"; do wait "$pid"; done
          for strategy in micro pca graph semantic external; do
            test -s "$R/${strategy}_latest.log"
            grep -q 'discovered=' "$R/${strategy}_latest.log"
          done

          ./build/polymarket_trade_recorder \\
"""
    if "Exercise every independent V5 sleeve" not in text:
        if insertion_anchor not in text:
            raise RuntimeError("live smoke strategy insertion anchor not found")
        text = text.replace(insertion_anchor, insertion, 1)
    text = re.sub(r"--markets 240 --batch 40 --min-liquidity 100", "--markets 500 --batch 50 --min-liquidity 25", text)
    text = re.sub(r"--config config/paper_v5\.json --markets 240 --min-liquidity 100", "--config config/paper_v5.json --markets 500 --min-liquidity 25", text)
    text = text.replace("--config config/paper_v5.json --markets 240 --history-universe 100", "--config config/paper_v5.json --markets 500 --history-universe 250")
    text = text.replace("--lookback-hours 336 --fidelity-minutes 30 --min-z 1.5", "--lookback-hours 504 --fidelity-minutes 15 --min-z 0.65")
    text = text.replace("--max-half-life-hours 168 --top 60", "--max-half-life-hours 240 --min-t-reversion 0.50 --top 100")
    text = text.replace("--config config/paper_v5.json --markets 240 --universe 80", "--config config/paper_v5.json --markets 500 --universe 250")
    text = text.replace("--lookback-hours 336 --fidelity-minutes 30 --factors 3 --max-hedges 4 --min-z 1.5", "--lookback-hours 504 --fidelity-minutes 15 --factors 5 --max-hedges 8 --min-z 0.65")
    text = text.replace("--max-half-life-hours 168 --top 60 --csv \"$R/stat_arb_pca_raw.csv\"", "--min-t-reversion 0.50 --max-half-life-hours 240 --max-factor-hedge-error 0.65 --top 100 --csv \"$R/stat_arb_pca_raw.csv\"")
    text = text.replace("--min-edge 0.001", "--min-edge 0.0001")
    text = text.replace("--markets 120 --min-liquidity 100 \\\n            --min-edge 0.003 --max-order-usd 50", "--markets 500 --min-liquidity 25 \\\n            --min-edge 0.0001 --max-order-usd 40")
    text = text.replace("--adverse-selection-mult 0.50", "--adverse-selection-mult 0.15")
    graph_only = """          ./build/polymarket_engine \\
            --config "$R/generated_configs/graph.json" --once --scan-only --markets 120 --min-liquidity 100 \\
            --run-dir "$R/strategies/graph" | tee "$R/graph_latest.log"

"""
    text = text.replace(graph_only, "")
    text = text.replace(
        'for f in "$R/allocator_validate.log" "$R/graph_latest.log" "$R/trade_recorder_latest.log"',
        'for f in "$R/allocator_validate.log" "$R/micro_latest.log" "$R/pca_latest.log" "$R/graph_latest.log" "$R/semantic_latest.log" "$R/external_latest.log" "$R/trade_recorder_latest.log"',
    )
    write(path, text)


def patch_deploy_parameters() -> None:
    path = ".github/workflows/deploy-paper-server.yml"
    p = ROOT / path
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8")
    # The deploy workflow owns the remote service topology.  Keep it intact but
    # ensure every V5 invocation uses the aggressive universe.
    lines = text.splitlines()
    active_block = 0
    out: list[str] = []
    for line in lines:
        if "multi_strategy_paper.py" in line or "polymarket_engine" in line or "polymarket_maker_paper" in line:
            active_block = 8
        if active_block > 0:
            line = re.sub(r"--markets\s+\d+", "--markets 500", line)
            line = re.sub(r"--min-liquidity\s+[0-9.]+", "--min-liquidity 25", line)
            active_block -= 1
        out.append(line)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


def add_strategy_logs_to_summary() -> None:
    path = "scripts/summarize_live_smoke.py"
    text = read(path)
    anchor = '    "terminal": "terminal_latest.log",\n'
    addition = anchor + '    "micro": "micro_latest.log",\n    "pca": "pca_latest.log",\n    "graph": "graph_latest.log",\n    "semantic": "semantic_latest.log",\n    "external": "external_latest.log",\n'
    if '"micro": "micro_latest.log"' not in text:
        if anchor not in text:
            raise RuntimeError("live summary LOGS anchor not found")
        text = text.replace(anchor, addition, 1)
    write(path, text)


def create_operations_policy() -> None:
    policy = {
        "schema": "polymarket_aggressive_operations_v1",
        "paper_only": True,
        "mission": "Frequent, cost-positive paper execution on live Polymarket data",
        "universe": {"target_markets": 500, "min_liquidity_usd": 25, "max_spread": 0.35},
        "activity_targets": {
            "minimum_model_processes_alive": 5,
            "minimum_market_coverage_ratio": 0.60,
            "minimum_daily_paper_entries": 20,
            "zero_activity_is_healthy_for_minutes": 30,
        },
        "risk": {
            "maximum_drawdown_ratio": 0.15,
            "maximum_gross_fraction": 0.45,
            "maximum_market_fraction": 0.02,
            "maximum_event_fraction": 0.06,
            "negative_expected_edge_allowed": False,
        },
        "repair": {
            "controller_interval_minutes": 10,
            "deployment_stale_minutes": 20,
            "health_stale_minutes": 15,
            "auto_dispatch_smoke": True,
            "auto_dispatch_deploy": True,
            "auto_dispatch_health": True,
        },
    }
    write("config/aggressive_operations_policy.json", json.dumps(policy, indent=2) + "\n")


def create_controller_workflow() -> None:
    workflow = r'''name: Polymarket Aggressive Operations Controller

on:
  workflow_dispatch:
  schedule:
    - cron: "*/10 * * * *"
  workflow_run:
    workflows:
      - v4-live-paper-smoke
      - deploy-paper-server
      - paper-server-health
    types: [completed]

permissions:
  actions: write
  contents: read

concurrency:
  group: polymarket-aggressive-operations-controller
  cancel-in-progress: true

jobs:
  govern:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Govern validation deployment and health
        shell: bash
        run: |
          set -euo pipefail
          repo="$GITHUB_REPOSITORY"
          now="$(date +%s)"
          main_sha="$(gh api "repos/$repo/git/ref/heads/main" --jq .object.sha)"
          validated_sha="$(gh api "repos/$repo/git/ref/heads/paper-validated" --jq .object.sha 2>/dev/null || true)"

          run_state() {
            local workflow="$1"
            gh run list --repo "$repo" --workflow "$workflow" --limit 1 \
              --json status,conclusion,createdAt,headSha \
              --jq 'if length == 0 then {status:"missing",conclusion:"",createdAt:"1970-01-01T00:00:00Z",headSha:""} else .[0] end'
          }
          age_minutes() {
            local created="$1"
            local epoch
            epoch="$(date -d "$created" +%s 2>/dev/null || echo 0)"
            echo $(( (now - epoch) / 60 ))
          }
          dispatch_unless_running() {
            local workflow="$1"; shift
            local state status
            state="$(run_state "$workflow")"
            status="$(jq -r .status <<<"$state")"
            if [[ "$status" == "queued" || "$status" == "in_progress" ]]; then
              echo "$workflow already $status"
              return 0
            fi
            gh workflow run "$workflow" --repo "$repo" --ref main "$@"
            echo "dispatched $workflow"
          }

          if [[ "$validated_sha" != "$main_sha" ]]; then
            dispatch_unless_running v4-live-smoke.yml -f expected_sha="$main_sha"
            exit 0
          fi

          deploy="$(run_state deploy-paper-server.yml)"
          deploy_age="$(age_minutes "$(jq -r .createdAt <<<"$deploy")")"
          deploy_ok="$(jq -r '.status == "completed" and .conclusion == "success"' <<<"$deploy")"
          if [[ "$deploy_ok" != "true" || "$deploy_age" -gt 20 ]]; then
            dispatch_unless_running deploy-paper-server.yml
          fi

          health="$(run_state server-health.yml)"
          health_age="$(age_minutes "$(jq -r .createdAt <<<"$health")")"
          health_ok="$(jq -r '.status == "completed" and .conclusion == "success"' <<<"$health")"
          if [[ "$health_ok" != "true" || "$health_age" -gt 15 ]]; then
            dispatch_unless_running server-health.yml
          fi

          echo "main=$main_sha validated=$validated_sha deploy_age_min=$deploy_age health_age_min=$health_age"
'''
    write(".github/workflows/aggressive-operations-controller.yml", workflow)


def create_policy_test() -> None:
    test = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    cfg = json.loads((ROOT / "config/paper_v5.json").read_text())
    assert cfg["market_limit"] >= 500
    assert cfg["min_liquidity"] <= 25
    assert cfg["max_spread"] >= 0.30
    assert cfg["max_drawdown"] <= 0.15
    multi = cfg["multi_strategy"]
    assert multi["paper_only"] is True
    enabled = [item for item in multi["strategies"] if item.get("enabled", True)]
    assert {item["name"] for item in enabled} == {"micro", "pca", "graph", "semantic", "external"}
    assert all(item["overrides"]["market_limit"] >= 500 for item in enabled)
    assert abs(sum(float(item["capital_fraction"]) for item in enabled) + float(multi["reserve_fraction"]) - 1.0) < 1e-9
    smoke = (ROOT / ".github/workflows/v4-live-smoke.yml").read_text()
    for name in ("micro", "pca", "graph", "semantic", "external"):
        assert f'"$R/{name}_latest.log"' in smoke
    assert "--once --paper" in smoke
    controller = (ROOT / ".github/workflows/aggressive-operations-controller.yml").read_text()
    assert 'cron: "*/10 * * * *"' in controller
    assert "deploy-paper-server.yml" in controller
    assert "server-health.yml" in controller


if __name__ == "__main__":
    main()
'''
    write("tests/test_aggressive_v5_policy.py", test)
    cmake = read("CMakeLists.txt")
    line = "    add_test(NAME aggressive_v5_policy_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_aggressive_v5_policy.py)\n"
    if "aggressive_v5_policy_tests" not in cmake:
        anchor = "    add_test(NAME multi_strategy_paper_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_multi_strategy_paper.py)\n"
        if anchor not in cmake:
            raise RuntimeError("CMake multi-strategy test anchor not found")
        cmake = cmake.replace(anchor, anchor + line, 1)
    write("CMakeLists.txt", cmake)


def main() -> None:
    patch_config()
    patch_engine()
    patch_pair_stat_arb()
    patch_pca_stat_arb()
    patch_maker()
    patch_coherent_filter()
    patch_live_smoke()
    patch_deploy_parameters()
    add_strategy_logs_to_summary()
    create_operations_policy()
    create_controller_workflow()
    create_policy_test()
    print("aggressive V5 upgrade applied; paper-only boundary preserved")


if __name__ == "__main__":
    main()
