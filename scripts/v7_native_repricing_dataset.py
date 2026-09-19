#!/usr/bin/env python3
"""Build native midpoint diagnostics with explicit capture and clock provenance.

This is NOT an executable-PnL dataset. Default CLI inputs must be sealed native
captures. Legacy/unsealed inspection is explicit and cannot qualify execution.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from v7_research_causal_dataset import strict_json
from v7_research_economic_contract import canonical_hash

SCHEMA = 'polymarket_v7_native_repricing_label_v1'
ORIGIN_REASONS = {1, 15, 16, 17}
HORIZONS = {100, 250, 500, 1000}
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError('invalid integer or missing clock')
    return value


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('missing capture identity')
    return value


def _signature(path: Path) -> tuple[int, ...]:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _source(path: Path, require_closed: bool) -> tuple[list[dict], dict]:
    if path.is_symlink() or not path.is_file():
        raise ValueError('unsafe native source')
    before = _signature(path)
    records: list[dict] = []
    hasher = hashlib.sha256()
    total = 0
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as stream:
        while True:
            raw = stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            total += len(raw)
            if total > MAX_SOURCE_BYTES or len(raw) > MAX_LINE_BYTES or not raw.endswith(b'\n'):
                raise ValueError('native source size limit or incomplete record')
            hasher.update(raw)
            row = strict_json(raw)
            if not isinstance(row, dict):
                raise ValueError('native record is not an object')
            records.append(row)
    if before != _signature(path):
        raise ValueError('native source mutated during read')
    if not records:
        raise ValueError('empty native source')
    # Compression preserves the producer sidecar name of the original JSONL.
    raw_path = path.with_suffix('') if path.suffix == '.gz' else path
    closed_path = Path(str(raw_path) + '.closed.json')
    closed = None
    if closed_path.exists():
        if closed_path.is_symlink() or closed_path.stat().st_size > 65536:
            raise ValueError('unsafe capture closure')
        closed_before = _signature(closed_path)
        closed = strict_json(closed_path.read_bytes())
        if closed_before != _signature(closed_path) or not isinstance(closed, dict):
            raise ValueError('capture closure changed or invalid')
        first = records[0]
        if (closed.get('schema') != 'polymarket_v7_native_capture_closed_v1'
                or closed.get('closed') is not True or closed.get('healthy') is not True
                or _integer(closed.get('bytes'), 1) != total
                or _integer(closed.get('last_sequence'), 1) != len(records)):
            raise ValueError('capture closure incomplete or unhealthy')
        for name in ('run_id', 'server_id', 'market_id', 'capture_id', 'code_sha'):
            expected = _identity(first.get(name))
            if closed.get(name) != expected or any(row.get(name) != expected for row in records):
                raise ValueError('capture closure identity mismatch:' + name)
        previous_observed = 0
        for sequence, row in enumerate(records, 1):
            if _integer(row.get('sequence'), 1) != sequence:
                raise ValueError('capture sequence gap or duplicate')
            if (row.get('schema') != 'polymarket_v7_native_observation_v1'
                    or row.get('paper_only') is not True or row.get('execution_authority') is not False):
                raise ValueError('invalid record inside certified capture')
            observed = _integer(row.get('observed_monotonic_ns'), 1)
            if observed < previous_observed:
                raise ValueError('capture observation clock moved backwards')
            previous_observed = observed
        if _integer(closed.get('watermark_monotonic_ns'), 1) != max(
                _integer(row.get('observed_monotonic_ns'), 1) for row in records):
            raise ValueError('capture watermark mismatch')
        if any(row.get('capture_semantics_version') != 2 or row.get('capture_mode') not in
               {'FULL', 'DECISION_WINDOWS'} for row in records):
            raise ValueError('unsupported capture semantics')
    elif require_closed:
        raise ValueError('missing producer closure: ' + str(closed_path))
    return records, {
        'path': str(path), 'decoded_sha256': hasher.hexdigest(), 'decoded_bytes': total,
        'records': len(records), 'producer_closed': closed is not None,
        'closure_sha256': canonical_hash(closed) if closed is not None else None,
        'capture_id': records[0].get('capture_id'),
    }


def _probability(row: dict[str, Any]) -> float | None:
    if row.get('repricing_pair_valid') is not True:
        return None
    try:
        yb, ya, nb, na = (_integer(row[k], 1) for k in (
            'yes_bid_e4', 'yes_ask_e4', 'no_bid_e4', 'no_ask_e4'))
        tick = _integer(row.get('tick_e4'), 1)
    except (KeyError, ValueError):
        return None
    if not (yb < ya < 10000 and nb < na < 10000 and tick < 10000):
        return None
    if any(p % tick for p in (yb, ya, nb, na)):
        return None
    yes, no = (yb + ya) / 20000, (nb + na) / 20000
    if abs(yes + no - 1) > 2 * tick / 10000 + 1e-9:
        return None
    p = (yes + 1 - no) / 2
    return p if 0 < p < 1 else None


def build(paths: list[Path], *, require_closed: bool = False) -> tuple[list[dict], dict]:
    """Legacy API defaults to diagnostic inspection; the CLI defaults to sealed.

    Joins never cross code/run/server/capture/market identities. Without a
    capture ID, one input file is one diagnostic scope; files are never joined.
    """
    origins, labels, gaps = {}, {}, defaultdict(list)
    sources, seen_paths, seen_hashes, seen_captures = [], set(), set(), set()
    invalid, total_bytes = 0, 0
    for path in map(Path, paths):
        resolved = path.resolve()
        if resolved in seen_paths:
            raise ValueError('duplicate native source path')
        seen_paths.add(resolved)
        rows, source = _source(path, require_closed)
        if source['decoded_sha256'] in seen_hashes:
            raise ValueError('duplicate native source content')
        seen_hashes.add(source['decoded_sha256'])
        if source['producer_closed']:
            capture_key = tuple(rows[0][k] for k in (
                'code_sha', 'run_id', 'server_id', 'capture_id', 'market_id'))
            if capture_key in seen_captures:
                raise ValueError('duplicate closed capture identity')
            seen_captures.add(capture_key)
        sources.append(source)
        total_bytes += source['decoded_bytes']
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('native dataset total input budget exceeded')
        for row in rows:
            if (row.get('schema') != 'polymarket_v7_native_observation_v1'
                    or row.get('paper_only') is not True or row.get('execution_authority') is not False):
                invalid += 1
                continue
            try:
                run, market = _identity(row.get('run_id')), _identity(row.get('market_id'))
                code = _identity(row.get('code_sha'))
                if len(code) != 40 or any(c not in '0123456789abcdef' for c in code):
                    raise ValueError('code identity')
                kind = _integer(row.get('kind'), 1)
                observed = _integer(row.get('observed_monotonic_ns'), 1)
                capture = row.get('capture_id')
                scope = (_identity(capture) if capture else 'UNIDENTIFIED_SOURCE:' + source['decoded_sha256'])
                server = row.get('server_id') or 'UNIDENTIFIED_SERVER'
                partition = (code, run, server, scope, market)
                if kind == 5:
                    gaps[partition].append(observed)
                    continue
                version = _integer(row.get('repricing_origin_signal_version'), 1)
                key = (*partition, version)
            except ValueError:
                invalid += 1
                continue
            destination, index = None, key
            if kind == 2 and row.get('reason') in ORIGIN_REASONS:
                destination = origins
            elif kind == 6:
                h = row.get('repricing_horizon_ms')
                if type(h) is not int or h not in HORIZONS:
                    invalid += 1
                    continue
                destination, index = labels, (*key, h)
            if destination is not None:
                if index in destination and destination[index][0] != row:
                    raise ValueError('conflicting repricing identities')
                destination[index] = (row, source)
    for values in gaps.values():
        values.sort()
    out, excluded = [], Counter()
    for key, (origin, origin_source) in sorted(origins.items()):
        p0 = _probability(origin)
        try:
            decision_ns = _integer(origin.get('decision_monotonic_ns'), 1)
            if _integer(origin.get('observed_monotonic_ns'), 1) < decision_ns:
                raise ValueError('origin observation precedes decision')
            external = origin.get('external_features')
            if external is None:
                external = {}
            if not isinstance(external, dict):
                raise ValueError('external feature shape')
            external_clock = external.get('input_receive_ns')
            if external and (type(external_clock) is not int or not 0 < external_clock <= decision_ns):
                raise ValueError('future or missing external feature clock')
        except ValueError:
            excluded['INVALID_ORIGIN_CLOCK_OR_FEATURE_CUT'] += len(HORIZONS)
            continue
        for h in sorted(HORIZONS):
            pair = labels.get((*key, h))
            label, label_source = pair if pair else ({}, {})
            p1 = _probability(label)
            if p0 is None or p1 is None:
                excluded['MISSING_OR_INVALID_PAIR'] += 1
                continue
            target_ns = decision_ns + h * 1000000
            try:
                observed_ns = _integer(label.get('observed_monotonic_ns'), 1)
                if observed_ns < target_ns or label.get('decision_monotonic_ns') != decision_ns:
                    raise ValueError('label horizon or decision mismatch')
                for field in ('asset', 'horizon', 'connection_epoch', 'tick_e4', 'close_monotonic_ns'):
                    if origin.get(field) != label.get(field):
                        raise ValueError('label context or epoch mismatch')
                close = origin.get('close_monotonic_ns')
                if close is not None and target_ns >= _integer(close, decision_ns + 1):
                    raise ValueError('target after market close')
                if label.get('paper_terms_sha256') != origin.get('paper_terms_sha256'):
                    raise ValueError('market terms changed')
                points = gaps[key[:-1]]
                j = bisect_right(points, decision_ns)
                if j < len(points) and points[j] <= observed_ns:
                    raise ValueError('book lineage gap')
            except ValueError:
                excluded['LABEL_CONTEXT_CLOCK_OR_CONTINUITY'] += 1
                continue
            out.append({
                'schema': SCHEMA, 'label_evidence_semantics_version': 2,
                'paper_only': True, 'authenticated_execution': False, 'real_order_submission': False,
                'execution_authority': 'ZERO_AUTHORITY_RESEARCH_ONLY',
                'model_sha': key[0], 'run_id': key[1], 'server_id': origin.get('server_id'),
                'capture_id': origin.get('capture_id'), 'market_id': key[4], 'origin_signal_version': key[5],
                'asset': origin.get('asset'), 'horizon': origin.get('horizon'), 'repricing_horizon_ms': h,
                'decision_wall_ns': origin.get('decision_wall_ns'), 'decision_monotonic_ns': decision_ns,
                'label_observed_monotonic_ns': observed_ns, 'target_monotonic_ns': target_ns,
                'origin_pm_yes': p0, 'label_pm_yes': p1, 'delta_probability': p1 - p0,
                'delta_logit': math.log(p1 / (1-p1)) - math.log(p0 / (1-p0)),
                'binance_return_100ms_bp': origin.get('binance_return_100ms_bp'),
                'confirmation_return_100ms_bp': origin.get('confirmation_return_100ms_bp'),
                'confirmation_venue': origin.get('confirmation_venue'), 'signal_age_ns': origin.get('signal_age_ns'),
                'tte_ns': origin.get('tte_ns'), 'external_features': external,
                'origin_pair': {k: origin.get(k) for k in ('yes_bid_e4', 'yes_ask_e4', 'no_bid_e4', 'no_ask_e4')},
                'label_pair': {k: label.get(k) for k in ('yes_bid_e4', 'yes_ask_e4', 'no_bid_e4', 'no_ask_e4')},
                'origin_source_sha256': origin_source['decoded_sha256'],
                'label_source_sha256': label_source['decoded_sha256'],
                'origin_record_sha256': canonical_hash(origin), 'label_record_sha256': canonical_hash(label),
                'producer_closed': bool(origin_source['producer_closed'] and label_source['producer_closed']),
                'target_semantics': 'CAUSAL_PM_BOOK_ASOF_NOMINAL_HORIZON_RECEIVE_TIME',
                'research_role': 'MIDPOINT_DIAGNOSTIC_NOT_EXECUTABLE_PNL',
                'eligible_for_executable_training': False,
                'label_availability_semantics': 'OBSERVATION_TIME_NOT_DURABLE_PUBLICATION_TIME',
            })
    out.sort(key=lambda r: (r['model_sha'], r['run_id'], str(r['capture_id']),
                           r['decision_monotonic_ns'], r['market_id'], r['repricing_horizon_ms']))
    summary = {'schema': 'polymarket_v7_native_repricing_dataset_summary_v1', 'origins': len(origins),
               'labels': len(out), 'censored_horizons': sum(excluded.values()), 'invalid_records': invalid,
               'conflicts': 0, 'horizons_ms': sorted(HORIZONS), 'paper_only': True,
               'sources': sources, 'excluded': dict(excluded), 'require_closed': require_closed,
               'executable_pnl_claim': False}
    return out, summary


def _publish(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.native-dataset-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic, never overwrite old evidence.
    finally:
        os.unlink(temporary)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, action='append', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--summary', type=Path, required=True)
    p.add_argument('--allow-unsealed-diagnostics', action='store_true')
    a = p.parse_args()
    if a.output.resolve() == a.summary.resolve() or any(
            dest.exists() or dest.is_symlink() for dest in (a.output, a.summary)):
        p.error('outputs must be distinct new files')
    try:
        rows, summary = build(a.input, require_closed=not a.allow_unsealed_diagnostics)
        data = ''.join(json.dumps(r, separators=(',', ':'), sort_keys=True, allow_nan=False) + '\n' for r in rows)
        if len(data.encode()) > MAX_TOTAL_BYTES:
            raise ValueError('native dataset output budget exceeded')
        summary['output_sha256'] = hashlib.sha256(data.encode()).hexdigest()
        # Consumers require this summary and its matching hash. A failed second
        # publication leaves an explicit orphan, never silent replacement.
        _publish(a.output, data)
        _publish(a.summary, json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + '\n')
    except (OSError, ValueError) as exc:
        p.exit(2, str(exc) + '\n')
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
