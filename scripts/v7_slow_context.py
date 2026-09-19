#!/usr/bin/env python3
"""Cold-plane context publication. No orders, model promotion or risk authority."""
from __future__ import annotations
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

FIELDS = ('spot_composite', 'volatility_medium', 'volatility_slow', 'return_5s',
          'dispersion_bps', 'binance_funding', 'binance_open_interest',
          'bybit_funding', 'bybit_open_interest', 'deribit_funding',
          'deribit_open_interest', 'oracle_value', 'opening_reference')


def context_path(run_root: Path, market_id: str) -> Path:
    return run_root / 'control' / 'slow_context' / (hashlib.sha256(market_id.encode()).hexdigest() + '.json')


def read_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def build_context(external: dict[str, Any], oracle_status: dict[str, Any], *,
                  code_sha: str, run_id: str, market: dict[str, Any],
                  now_ns: int, wall_ns: int) -> dict[str, Any]:
    def obj(value):
        return value if isinstance(value, dict) else {}

    external, oracle_status = obj(external), obj(oracle_status)
    asset, horizon = str(market.get('asset') or ''), str(market.get('horizon') or '')
    market_id = str(market.get('market_id') or '')
    fields: dict[str, Any] = dict.fromkeys(FIELDS)

    def put(name: str, value: Any, receive: Any, version: Any, ttl: int,
            *, expiry: int | None = None, source: str) -> None:
        # Boolean values, missing clocks and stale values are not zero observations.
        if (isinstance(value, bool) or not isinstance(receive, int)
                or isinstance(receive, bool) or not isinstance(version, int)
                or isinstance(version, bool)):
            return
        try:
            v, r, seq = float(value), int(receive), int(version)
            end = r + ttl if expiry is None else min(r + ttl, expiry)
        except (ValueError, TypeError, OverflowError):
            return
        if not math.isfinite(v) or seq <= 0 or not 0 < r <= now_ns <= end:
            return
        fields[name] = {'value': v, 'receive_monotonic_ns': r,
                        'expires_monotonic_ns': end, 'source_version': seq, 'source': source}

    safe = (external.get('schema') == 'polymarket_v7_external_venue_runtime_v1'
            and external.get('code_sha') == code_sha and external.get('asset') == asset
            and external.get('paper_only') is True
            and external.get('authenticated_execution') is False
            and external.get('real_order_submission') is False)
    if safe and external.get('valid') is True:
        # Source clocks, not the timestamp of this file read/publication.
        receive = external.get('price_inputs_receive_monotonic_ns')
        expiry = external.get('price_inputs_valid_until_monotonic_ns')
        version = external.get('state_version')
        if isinstance(expiry, int) and not isinstance(expiry, bool):
            for name, key in [('spot_composite', 'composite_price'),
                              ('volatility_medium', 'realized_vol_medium'),
                              ('volatility_slow', 'realized_vol_slow'),
                              ('dispersion_bps', 'dispersion_bps')]:
                put(name, external.get(key), receive, version, 1_000_000_000,
                    expiry=expiry, source='CAUSAL_SPOT_COMPOSITE')
            history = obj(external.get('return_history_available'))
            if history.get('5s') is True:
                put('return_5s', external.get('return_5s'), receive, version,
                    1_000_000_000, expiry=expiry, source='CAUSAL_SPOT_COMPOSITE')
    if safe:
        derivatives = external.get('derivative_contexts')
        for derivative in derivatives if isinstance(derivatives, list) else []:
            if not isinstance(derivative, dict) or not isinstance(derivative.get('venue'), str):
                continue
            prefix = {'BINANCE_USDM': 'binance', 'BYBIT_LINEAR': 'bybit', 'DERIBIT': 'deribit'}.get(derivative.get('venue'))
            clocks = derivative.get('field_receive_monotonic_ns')
            if not prefix or derivative.get('healthy') is not True or derivative.get('gap') is not False:
                continue
            if not isinstance(clocks, list) or len(clocks) != 4:
                continue
            for index, suffix, key in [(2, 'funding', 'funding_rate'), (3, 'open_interest', 'open_interest_native')]:
                mask = derivative.get('valid_mask')
                if not isinstance(mask, int) or not 0 <= mask <= 15 or not (mask & (1 << index)):
                    continue
                put(prefix + '_' + suffix, derivative.get(key), clocks[index],
                    external.get('state_version'), 1_000_000_000,
                    source=str(derivative.get('venue')) + '_NATIVE_UNITS')
    # The current RTDS monitor is BTC/M5 only. Never reuse that reference for
    # another asset/horizon or replace the actual oracle with an exchange price.
    contract = obj(oracle_status.get('contract'))
    bound = (asset == 'BTC' and horizon == 'M5'
             and oracle_status.get('code_sha') == code_sha
             and obj(oracle_status.get('market')).get('market_id') == market_id
             and oracle_status.get('paper_only') is True
             and oracle_status.get('authenticated_execution') is False
             and oracle_status.get('real_order_submission') is False
             and contract.get('verified') is True and contract.get('rules_hash_recognized') is True)
    if bound:
        oracle = obj(oracle_status.get('oracle'))
        if oracle.get('healthy') is True:
            put('oracle_value', oracle.get('value'), oracle.get('receive_monotonic_ns'),
                oracle.get('source_sequence'), 2_000_000_000, source='CONTRACT_BOUND_RTDS_ORACLE')
        reference = obj(oracle_status.get('settlement_reference'))
        close = int(market.get('close_timestamp_unix') or 0)
        if not close:
            close = int(market.get('window_start_unix') or 0) + int(market.get('horizon_seconds') or 0)
        if reference.get('valid') is True and close * 1_000_000_000 > wall_ns:
            end = now_ns + close * 1_000_000_000 - wall_ns
            put('opening_reference', reference.get('value'), reference.get('receive_monotonic_ns'),
                reference.get('version'), 86_400_000_000_000, expiry=end,
                source=str(reference.get('provenance') or 'CONTRACT_BOUND_REFERENCE'))
    return {'schema': 'polymarket_v7_slow_context_v1', 'version': now_ns,
            'published_monotonic_ns': now_ns, 'code_sha': code_sha, 'run_id': run_id,
            'market_id': market_id, 'asset': asset, 'horizon': horizon,
            'paper_only': True, 'authenticated_execution': False, 'real_order_submission': False,
            'observation_only': True, 'fields': fields}


def publish_contexts(run_root: Path, *, code_sha: str, run_id: str,
                     markets: dict[str, dict[str, Any]]) -> int:
    """Called by the existing cold manager, never by the native decision loop."""
    sources: dict[str, dict[str, Any]] = {}
    oracle = read_json(run_root / 'external_fair/status.json')
    written = 0
    for market in markets.values():
        asset = str(market.get('asset') or '')
        if asset not in sources:
            base = run_root / 'external_fair'
            if asset != 'BTC':
                base = base / 'assets' / asset.lower()
            sources[asset] = read_json(base / 'external_venues.json')
        now_ns, wall_ns = time.monotonic_ns(), time.time_ns()
        value = build_context(sources[asset], oracle, code_sha=code_sha, run_id=run_id,
                              market=market, now_ns=now_ns, wall_ns=wall_ns)
        path = context_path(run_root, str(market['market_id']))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f'.tmp.{os.getpid()}')
        tmp.write_text(json.dumps(value, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(tmp, path)
        written += 1
    return written
