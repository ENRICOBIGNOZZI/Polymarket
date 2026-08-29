from __future__ import annotations
import argparse
import csv
import json
import math
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Mapping, MutableMapping, Sequence
EXPORTER_VERSION = '1.0.0'

def _float(value: object, default: float=0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default

def _int(value: object, default: int=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

def _prom_escape(value: object) -> str:
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')

def _finite(value: float) -> str:
    if math.isnan(value):
        return 'NaN'
    if math.isinf(value):
        return '+Inf' if value > 0 else '-Inf'
    return format(value, '.12g')

def _slug(value: object, limit: int=160) -> str:
    text = str(value or '')
    return text if len(text) <= limit else text[:limit - 1] + '…'

class Metrics:

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def _declare(self, name: str, help_text: str, metric_type: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self._lines.append(f'# HELP {name} {help_text}')
        self._lines.append(f'# TYPE {name} {metric_type}')

    def sample(self, name: str, value: float | int, *, help_text: str, metric_type: str='gauge', labels: Mapping[str, object] | None=None) -> None:
        self._declare(name, help_text, metric_type)
        suffix = ''
        if labels:
            encoded = ','.join((f'{key}="{_prom_escape(val)}"' for key, val in sorted(labels.items())))
            suffix = '{' + encoded + '}'
        self._lines.append(f'{name}{suffix} {_finite(float(value))}')

    def render(self) -> str:
        return '\n'.join(self._lines) + '\n'

@dataclass
class IncrementalState:
    inode: int | None = None
    offset: int = 0
    header: list[str] | None = None

class IncrementalCsvAggregate:

    def __init__(self, path: Path, on_row: Callable[[Mapping[str, str]], None], on_reset: Callable[[], None]) -> None:
        self.path = path
        self.on_row = on_row
        self.on_reset = on_reset
        self.state = IncrementalState()

    def reset(self) -> None:
        self.state = IncrementalState()
        self.on_reset()

    def refresh(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self.reset()
            return
        if self.state.inode != stat.st_ino or stat.st_size < self.state.offset:
            self.reset()
        self.state.inode = stat.st_ino
        with self.path.open('r', encoding='utf-8', newline='') as handle:
            if self.state.offset:
                handle.seek(self.state.offset)
                reader = csv.DictReader(handle, fieldnames=self.state.header)
            else:
                reader = csv.DictReader(handle)
                self.state.header = list(reader.fieldnames or [])
            for row in reader:
                if not row or not any(((value or '').strip() for value in row.values())):
                    continue
                self.on_row({str(k): str(v or '') for k, v in row.items() if k is not None})
            self.state.offset = handle.tell()

class LogAggregates:

    def __init__(self, run_root: Path) -> None:
        self.maker_fill_counts: MutableMapping[tuple[str, str], int] = defaultdict(int)
        self.maker_fill_fees = 0.0
        self.maker_fill_notional = 0.0
        self.maker_order_counts: MutableMapping[str, int] = defaultdict(int)
        self.terminal_fill_counts: MutableMapping[tuple[str, str], int] = defaultdict(int)
        self.terminal_fill_fees = 0.0
        self.terminal_fill_notional = 0.0
        self._maker_fills = IncrementalCsvAggregate(run_root / 'maker' / 'maker_fills.csv', self._consume_maker_fill, self._reset_maker_fills)
        self._maker_orders = IncrementalCsvAggregate(run_root / 'maker' / 'maker_order_log.csv', self._consume_maker_order, self._reset_maker_orders)
        self._terminal_fills = IncrementalCsvAggregate(run_root / 'terminal' / 'fills.csv', self._consume_terminal_fill, self._reset_terminal_fills)

    def _reset_maker_fills(self) -> None:
        self.maker_fill_counts.clear()
        self.maker_fill_fees = 0.0
        self.maker_fill_notional = 0.0

    def _reset_maker_orders(self) -> None:
        self.maker_order_counts.clear()

    def _reset_terminal_fills(self) -> None:
        self.terminal_fill_counts.clear()
        self.terminal_fill_fees = 0.0
        self.terminal_fill_notional = 0.0

    def _consume_maker_fill(self, row: Mapping[str, str]) -> None:
        action = row.get('action', 'UNKNOWN') or 'UNKNOWN'
        side = row.get('side', 'UNKNOWN') or 'UNKNOWN'
        self.maker_fill_counts[action, side] += 1
        shares = max(0.0, _float(row.get('shares')))
        price = max(0.0, _float(row.get('price')))
        self.maker_fill_notional += shares * price
        self.maker_fill_fees += max(0.0, _float(row.get('fee')))

    def _consume_maker_order(self, row: Mapping[str, str]) -> None:
        action = row.get('action', 'UNKNOWN') or 'UNKNOWN'
        self.maker_order_counts[action] += 1

    def _consume_terminal_fill(self, row: Mapping[str, str]) -> None:
        action = row.get('action', 'UNKNOWN') or 'UNKNOWN'
        side = row.get('side', 'UNKNOWN') or 'UNKNOWN'
        self.terminal_fill_counts[action, side] += 1
        self.terminal_fill_notional += max(0.0, _float(row.get('notional')))
        self.terminal_fill_fees += max(0.0, _float(row.get('fee')))

    def refresh(self) -> None:
        self._maker_fills.refresh()
        self._maker_orders.refresh()
        self._terminal_fills.refresh()

def _last_csv_row(path: Path) -> dict[str, str] | None:
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return None
            block = 4096
            data = b''
            position = size
            while position > 0 and data.count(b'\n') < 3:
                read_size = min(block, position)
                position -= read_size
                handle.seek(position)
                data = handle.read(read_size) + data
        lines = [line for line in data.decode('utf-8', errors='replace').splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        with path.open('r', encoding='utf-8', newline='') as handle:
            header = next(csv.reader(handle), [])
        values = next(csv.reader([lines[-1]]), [])
        if len(values) != len(header):
            return None
        return dict(zip(header, values))
    except (OSError, csv.Error):
        return None

def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open('r', encoding='utf-8', newline='') as handle:
            return [{str(k): str(v or '') for k, v in row.items() if k is not None} for row in csv.DictReader(handle) if row]
    except (FileNotFoundError, OSError, csv.Error):
        return []

def _read_json(path: Path) -> dict[str, object] | None:
    try:
        with path.open('r', encoding='utf-8') as handle:
            obj = json.load(handle)
        return obj if isinstance(obj, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None

def _structural_rows(path: Path) -> tuple[dict[str, float], list[dict[str, str]]]:
    summary: dict[str, float] = {}
    try:
        lines = [line.rstrip('\n') for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except OSError:
        return (summary, [])
    header_index = -1
    for index, line in enumerate(lines):
        if line.startswith('type,event_id,anchor,legs,'):
            header_index = index
            break
        if index == 0:
            for token in line.split():
                if '=' not in token:
                    continue
                key, value = token.split('=', 1)
                summary[key] = _float(value)
    if header_index < 0:
        return (summary, [])
    rows: list[dict[str, str]] = []
    reader = csv.DictReader(lines[header_index:])
    expected = set(reader.fieldnames or [])
    for row in reader:
        if not row or set(row) != expected or row.get('type', '').startswith('NOTE:'):
            continue
        rows.append({str(k): str(v or '') for k, v in row.items() if k is not None})
    return (summary, rows)

class Collector:

    def __init__(self, run_root: Path, config_path: Path, top_opportunities: int=20) -> None:
        self.run_root = run_root
        self.config_path = config_path
        self.top_opportunities = max(1, top_opportunities)
        self.logs = LogAggregates(run_root)
        self.lock = threading.Lock()

    def _config(self) -> tuple[float, float]:
        cfg = _read_json(self.config_path) or {}
        return (_float(cfg.get('starting_capital'), 10000.0), _float(cfg.get('max_drawdown'), 0.15))

    def collect(self) -> str:
        with self.lock:
            now = time.time()
            metrics = Metrics()
            errors = 0
            starting_capital, max_drawdown = self._config()
            metrics.sample('polymarket_exporter_info', 1, help_text='Static information about the Polymarket file exporter.', labels={'version': EXPORTER_VERSION, 'run_root': str(self.run_root)})
            try:
                self.logs.refresh()
            except Exception:
                errors += 1
            self._maker(metrics, now, starting_capital, max_drawdown)
            self._terminal(metrics, now, starting_capital, max_drawdown)
            self._strategies(metrics, now)
            self._log_metrics(metrics)
            metrics.sample('polymarket_exporter_scrape_errors', errors, help_text='Number of non-fatal errors encountered during the current exporter scrape.')
            return metrics.render()

    def _maker(self, metrics: Metrics, now: float, starting_capital: float, max_drawdown: float) -> None:
        maker_dir = self.run_root / 'maker'
        equity_path = maker_dir / 'maker_equity.csv'
        row = _last_csv_row(equity_path)
        metrics.sample('polymarket_maker_state_present', 1 if row else 0, help_text='Whether a complete paper-maker equity snapshot is currently available.')
        metrics.sample('polymarket_maker_max_drawdown_ratio', max_drawdown, help_text='Configured maximum paper-maker drawdown ratio.')
        if row:
            cash = _float(row.get('cash'))
            equity = _float(row.get('equity'))
            peak = _float(row.get('peak_equity'), max(equity, starting_capital))
            drawdown = _float(row.get('drawdown'))
            ts = _float(row.get('timestamp'), _mtime(equity_path) or now)
            fields = {'polymarket_maker_cash_usd': (cash, 'Current paper-maker cash in USD.'), 'polymarket_maker_equity_usd': (equity, 'Current marked paper-maker equity in USD.'), 'polymarket_maker_peak_equity_usd': (peak, 'Historical peak paper-maker equity in USD.'), 'polymarket_maker_reserved_cash_usd': (_float(row.get('reserved_cash')), 'Cash reserved by resting paper-maker orders in USD.'), 'polymarket_maker_resting_orders': (_float(row.get('resting_orders')), 'Current number of resting simulated maker orders.'), 'polymarket_maker_open_positions': (_float(row.get('positions')), 'Current number of paper-maker positions.'), 'polymarket_maker_drawdown_ratio': (drawdown, 'Current paper-maker drawdown ratio.'), 'polymarket_maker_kill_switch': (_float(row.get('killed')), 'Paper-maker drawdown kill switch state; one means active.'), 'polymarket_maker_pnl_usd': (equity - starting_capital, 'Paper-maker PnL versus configured starting capital.'), 'polymarket_maker_return_ratio': (equity / starting_capital - 1.0 if starting_capital > 0 else 0.0, 'Paper-maker return versus configured starting capital.'), 'polymarket_maker_last_update_timestamp_seconds': (ts, 'Unix timestamp of the latest paper-maker equity snapshot.'), 'polymarket_maker_staleness_seconds': (max(0.0, now - ts), 'Age in seconds of the latest paper-maker equity snapshot.')}
            for name, (value, help_text) in fields.items():
                metrics.sample(name, value, help_text=help_text)
        for row in _read_csv(maker_dir / 'maker_positions.csv'):
            market_id = row.get('market_id', '')
            labels = {'market_id': market_id, 'slug': _slug(row.get('slug', '')), 'side': row.get('side', '')}
            shares = max(0.0, _float(row.get('shares')))
            entry = max(0.0, _float(row.get('entry_price')))
            entry_ts = _float(row.get('entry_ts'), now)
            metrics.sample('polymarket_maker_position_notional_usd', shares * entry, help_text='Entry notional of each current paper-maker position.', labels=labels)
            metrics.sample('polymarket_maker_position_shares', shares, help_text='Share quantity of each current paper-maker position.', labels=labels)
            metrics.sample('polymarket_maker_position_entry_price', entry, help_text='Entry price of each current paper-maker position.', labels=labels)
            metrics.sample('polymarket_maker_position_age_seconds', max(0.0, now - entry_ts), help_text='Age in seconds of each current paper-maker position.', labels=labels)
        for row in _read_csv(maker_dir / 'maker_orders.csv'):
            labels = {'market_id': row.get('market_id', ''), 'slug': _slug(row.get('slug', '')), 'side': row.get('side', '')}
            shares = max(0.0, _float(row.get('shares')))
            price = max(0.0, _float(row.get('limit_price')))
            created = _float(row.get('created_ts'), now)
            metrics.sample('polymarket_maker_order_reserved_usd', shares * price, help_text='Cash reserved by each current simulated maker order.', labels=labels)
            metrics.sample('polymarket_maker_order_age_seconds', max(0.0, now - created), help_text='Age in seconds of each current simulated maker order.', labels=labels)

    def _terminal(self, metrics: Metrics, now: float, starting_capital: float, max_drawdown: float) -> None:
        terminal_dir = self.run_root / 'terminal'
        status_path = terminal_dir / 'status.json'
        status = _read_json(status_path)
        metrics.sample('polymarket_terminal_state_present', 1 if status else 0, help_text='Whether a terminal-sleeve status snapshot is currently available.')
        metrics.sample('polymarket_terminal_max_drawdown_ratio', max_drawdown, help_text='Configured maximum terminal-sleeve drawdown ratio.')
        if status:
            equity = _float(status.get('equity'))
            ts = _float(status.get('timestamp'), _mtime(status_path) or now)
            values = {'polymarket_terminal_cash_usd': (_float(status.get('cash')), 'Terminal-sleeve paper cash in USD.'), 'polymarket_terminal_equity_usd': (equity, 'Terminal-sleeve marked paper equity in USD.'), 'polymarket_terminal_peak_equity_usd': (_float(status.get('peak_equity')), 'Terminal-sleeve historical peak equity in USD.'), 'polymarket_terminal_gross_exposure_usd': (_float(status.get('gross_exposure')), 'Terminal-sleeve gross exposure in USD.'), 'polymarket_terminal_open_positions': (_float(status.get('open_positions')), 'Number of terminal-sleeve open paper positions.'), 'polymarket_terminal_drawdown_ratio': (_float(status.get('drawdown')), 'Current terminal-sleeve drawdown ratio.'), 'polymarket_terminal_kill_switch': (1.0 if bool(status.get('killed')) else 0.0, 'Terminal-sleeve kill switch state; one means active.'), 'polymarket_terminal_pnl_usd': (equity - starting_capital, 'Terminal-sleeve PnL versus configured starting capital.'), 'polymarket_terminal_return_ratio': (equity / starting_capital - 1.0 if starting_capital > 0 else 0.0, 'Terminal-sleeve return versus configured starting capital.'), 'polymarket_terminal_last_update_timestamp_seconds': (ts, 'Unix timestamp of the latest terminal-sleeve status snapshot.'), 'polymarket_terminal_staleness_seconds': (max(0.0, now - ts), 'Age in seconds of the latest terminal-sleeve status snapshot.')}
            for name, (value, help_text) in values.items():
                metrics.sample(name, value, help_text=help_text)
        for row in _read_csv(terminal_dir / 'broker_state.csv'):
            labels = {'market_id': row.get('market_id', ''), 'slug': _slug(row.get('slug', '')), 'side': row.get('side', '')}
            metrics.sample('polymarket_terminal_position_cost_basis_usd', max(0.0, _float(row.get('cost_basis'))), help_text='Cost basis of each current terminal-sleeve paper position.', labels=labels)
        signals = _read_csv(terminal_dir / 'signals.csv')
        signals.sort(key=lambda row: _float(row.get('score')), reverse=True)
        for row in signals[:self.top_opportunities]:
            labels = {'market_id': row.get('market_id', ''), 'slug': _slug(row.get('slug', '')), 'side': row.get('side', '')}
            for name, column, help_text in (('polymarket_terminal_signal_net_edge_ratio', 'net_edge', 'Current terminal signal net executable edge.'), ('polymarket_terminal_signal_score', 'score', 'Current terminal signal uncertainty-adjusted score.'), ('polymarket_terminal_signal_uncertainty', 'uncertainty', 'Current terminal signal uncertainty.'), ('polymarket_terminal_signal_desired_notional_usd', 'desired_notional', 'Desired terminal signal notional before final portfolio clipping.')):
                metrics.sample(name, _float(row.get(column)), help_text=help_text, labels=labels)

    def _strategies(self, metrics: Metrics, now: float) -> None:
        structural_path = self.run_root / 'structural_latest.csv'
        structural_summary, structural = _structural_rows(structural_path)
        self._strategy_clock(metrics, structural_path, now, 'structural')
        if structural_summary:
            for key in ('discovered', 'scanned_events', 'opportunities', 'raw_positive', 'net_positive_pre_gas'):
                if key in structural_summary:
                    metrics.sample('polymarket_structural_scan_total', structural_summary[key], help_text='Latest structural-arbitrage scan count by diagnostic field.', labels={'field': key})
        positive = [row for row in structural if _float(row.get('net_edge_pre_gas')) > 0.0]
        metrics.sample('polymarket_structural_positive_opportunities', len(positive), help_text='Number of latest structural opportunities with positive pre-gas net edge.')
        max_edge = max((_float(row.get('net_edge_pre_gas')) for row in structural), default=0.0)
        max_profit = max((_float(row.get('estimated_profit_pre_gas')) for row in structural), default=0.0)
        metrics.sample('polymarket_structural_max_net_edge_ratio', max_edge, help_text='Maximum latest executable structural net edge before gas and latency.', labels={'basis': 'pre_gas'})
        metrics.sample('polymarket_structural_max_estimated_profit_usd', max_profit, help_text='Maximum latest estimated structural profit before gas and latency.', labels={'basis': 'pre_gas'})
        structural.sort(key=lambda row: _float(row.get('net_edge_pre_gas')), reverse=True)
        for row in structural[:self.top_opportunities]:
            labels = {'type': row.get('type', ''), 'event_id': row.get('event_id', ''), 'anchor': _slug(row.get('anchor', ''))}
            metrics.sample('polymarket_structural_opportunity_net_edge_ratio', _float(row.get('net_edge_pre_gas')), help_text='Latest structural opportunity net edge before gas and latency.', labels=labels)
            metrics.sample('polymarket_structural_opportunity_profit_usd', _float(row.get('estimated_profit_pre_gas')), help_text='Latest structural opportunity estimated profit before gas and latency.', labels=labels)
        pair_path = self.run_root / 'stat_arb_pairs.csv'
        pair_rows = _read_csv(pair_path)
        self._stat_arb(metrics, now, pair_path, 'pair', pair_rows)
        pca_path = self.run_root / 'stat_arb_pca.csv'
        pca_rows = _read_csv(pca_path)
        self._stat_arb(metrics, now, pca_path, 'pca', pca_rows)

    def _strategy_clock(self, metrics: Metrics, path: Path, now: float, sleeve: str) -> None:
        modified = _mtime(path)
        metrics.sample('polymarket_strategy_state_present', 1 if modified is not None else 0, help_text='Whether the latest strategy diagnostic file is available.', labels={'sleeve': sleeve})
        if modified is None:
            return
        metrics.sample('polymarket_strategy_last_update_timestamp_seconds', modified, help_text='Unix timestamp of the latest strategy diagnostic file update.', labels={'sleeve': sleeve})
        metrics.sample('polymarket_strategy_staleness_seconds', max(0.0, now - modified), help_text='Age in seconds of the latest strategy diagnostic file.', labels={'sleeve': sleeve})

    def _stat_arb(self, metrics: Metrics, now: float, path: Path, sleeve: str, rows: Sequence[Mapping[str, str]]) -> None:
        self._strategy_clock(metrics, path, now, sleeve)
        for basis, column in (('taker', 'taker_net_edge'), ('maker', 'maker_entry_net_edge')):
            positive = sum((1 for row in rows if _float(row.get(column)) > 0.0))
            maximum = max((_float(row.get(column)) for row in rows), default=0.0)
            metrics.sample('polymarket_stat_arb_positive_opportunities', positive, help_text='Number of latest statistical-arbitrage opportunities with positive net edge.', labels={'sleeve': sleeve, 'basis': basis})
            metrics.sample('polymarket_stat_arb_max_net_edge_ratio', maximum, help_text='Maximum latest statistical-arbitrage net edge.', labels={'sleeve': sleeve, 'basis': basis})
        ordered = sorted(rows, key=lambda row: _float(row.get('maker_entry_net_edge')), reverse=True)
        for row in ordered[:self.top_opportunities]:
            if sleeve == 'pair':
                opportunity_id = f"{row.get('y_market', '')}:{row.get('x_market', '')}"
                labels = {'sleeve': sleeve, 'opportunity_id': opportunity_id, 'relation': row.get('relation', ''), 'side': f"{row.get('y_side', '')}/{row.get('x_side', '')}", 'slug': _slug(f"{row.get('y_slug', '')} | {row.get('x_slug', '')}")}
            else:
                opportunity_id = row.get('market', '')
                labels = {'sleeve': sleeve, 'opportunity_id': opportunity_id, 'relation': 'factor_neutral', 'side': row.get('side', ''), 'slug': _slug(row.get('slug', ''))}
            for basis, column in (('taker', 'taker_net_edge'), ('maker', 'maker_entry_net_edge')):
                metrics.sample('polymarket_stat_arb_opportunity_net_edge_ratio', _float(row.get(column)), help_text='Latest statistical-arbitrage opportunity net edge.', labels={**labels, 'basis': basis})
            metrics.sample('polymarket_stat_arb_opportunity_zscore', _float(row.get('z') if sleeve == 'pair' else row.get('residual_z')), help_text='Latest statistical-arbitrage opportunity residual z-score.', labels=labels)
            metrics.sample('polymarket_stat_arb_opportunity_half_life_hours', _float(row.get('half_life_h')), help_text='Estimated mean-reversion half-life for a statistical-arbitrage opportunity.', labels=labels)
            metrics.sample('polymarket_stat_arb_opportunity_executable_notional_usd', _float(row.get('executable_notional')), help_text='Displayed executable notional for a statistical-arbitrage opportunity.', labels=labels)

    def _log_metrics(self, metrics: Metrics) -> None:
        for (action, side), count in sorted(self.logs.maker_fill_counts.items()):
            metrics.sample('polymarket_maker_fills_total', count, help_text='Cumulative simulated paper-maker fills by action and side.', metric_type='counter', labels={'action': action, 'side': side})
        metrics.sample('polymarket_maker_fees_paid_usd_total', self.logs.maker_fill_fees, help_text='Cumulative paper-maker simulated fees paid in USD.', metric_type='counter')
        metrics.sample('polymarket_maker_traded_notional_usd_total', self.logs.maker_fill_notional, help_text='Cumulative paper-maker simulated traded notional in USD.', metric_type='counter')
        for action, count in sorted(self.logs.maker_order_counts.items()):
            metrics.sample('polymarket_maker_order_events_total', count, help_text='Cumulative simulated maker order lifecycle events.', metric_type='counter', labels={'action': action})
        for (action, side), count in sorted(self.logs.terminal_fill_counts.items()):
            metrics.sample('polymarket_terminal_fills_total', count, help_text='Cumulative terminal-sleeve simulated fills by action and side.', metric_type='counter', labels={'action': action, 'side': side})
        metrics.sample('polymarket_terminal_fees_paid_usd_total', self.logs.terminal_fill_fees, help_text='Cumulative terminal-sleeve simulated fees paid in USD.', metric_type='counter')
        metrics.sample('polymarket_terminal_traded_notional_usd_total', self.logs.terminal_fill_notional, help_text='Cumulative terminal-sleeve simulated traded notional in USD.', metric_type='counter')

class ExporterHandler(BaseHTTPRequestHandler):
    collector: Collector

    def do_GET(self) -> None:
        if self.path == '/metrics':
            body = self.collector.collect().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/healthz':
            body = b'ok\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:
        return

def parse_args(argv: Sequence[str] | None=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs-root', type=Path, default=Path('runs/paper_v3_live'))
    parser.add_argument('--config', type=Path, default=Path('config/paper_v3.json'))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9108)
    parser.add_argument('--top-opportunities', type=int, default=20)
    return parser.parse_args(argv)

def main(argv: Sequence[str] | None=None) -> int:
    args = parse_args(argv)
    collector = Collector(args.runs_root, args.config, args.top_opportunities)
    ExporterHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), ExporterHandler)
    print(f'polymarket exporter listening on http://{args.host}:{args.port}/metrics', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
