#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from v7_multi_crypto_execution_accounting_contract import validate


def test_repository_contract_is_single_owner_zero_authority() -> None:
    value = validate(ROOT)
    assert value['state'] == 'VERIFIED_SHADOW_CONTRACT'
    assert value['new_risk_authorized'] is False
    assert value['single_global_execution_owner'] is True
    assert value['single_canonical_ledger_writer'] is True
    assert value['global_portfolio_coordinator'] == 'V7_GLOBAL_PORTFOLIO_COORDINATOR'
    assert value['ledger_owner'] == 'V7_CANONICAL_LEDGER'


def mutated_root(filename: str, mutate):
    tmp = tempfile.TemporaryDirectory(); root = Path(tmp.name)
    (root / 'config').mkdir(); (root / 'scripts').mkdir()
    for name in ('paper_v7.json','v7_authority_registry.json','v7_multi_crypto_assets.json',
                 'v7_crypto_settlement_markets.json','v7_multi_crypto_shadow_runtime.json'):
        value=json.loads((ROOT/'config'/name).read_text()); mutate(value) if name==filename else None
        (root/'config'/name).write_text(json.dumps(value))
    for name in ('v7_multi_crypto_shadow_runtime.py','v7_opportunity.py'):
        (root/'scripts'/name).write_text((ROOT/'scripts'/name).read_text())
    return tmp, root


def test_second_ledger_writer_is_rejected() -> None:
    def mutate(value): value['multi_strategy']['single_canonical_ledger_writer'] = False
    tmp, root = mutated_root('paper_v7.json', mutate)
    try:
        try: validate(root)
        except ValueError as exc: assert 'ledger_writer' in str(exc)
        else: raise AssertionError('second ledger writer contract accepted')
    finally: tmp.cleanup()


def test_wrong_global_coordinator_is_rejected() -> None:
    def mutate(value): value['owners']['global_portfolio_coordinator'] = 'OTHER'
    tmp, root = mutated_root('v7_authority_registry.json', mutate)
    try:
        try: validate(root)
        except ValueError as exc: assert 'owner_mismatch' in str(exc)
        else: raise AssertionError('wrong coordinator accepted')
    finally: tmp.cleanup()


def test_asset_entry_authority_is_rejected() -> None:
    def mutate(value): value['assets'][1]['allow_new_entry_authority'] = True
    tmp, root = mutated_root('v7_multi_crypto_assets.json', mutate)
    try:
        try: validate(root)
        except ValueError as exc: assert 'new_entry_authority' in str(exc)
        else: raise AssertionError('new lane authority accepted')
    finally: tmp.cleanup()


def test_non_btc_settlement_authority_is_rejected() -> None:
    def mutate(value):
        row = next(r for r in value['contexts'] if r['asset'] == 'ETH' and r['horizon'] == 'M5')
        row['authority'] = 'PAPER'
    tmp, root = mutated_root('v7_crypto_settlement_markets.json', mutate)
    try:
        try: validate(root)
        except ValueError as exc: assert 'non_btc_authority' in str(exc)
        else: raise AssertionError('ETH execution authority accepted')
    finally: tmp.cleanup()


if __name__ == '__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _, fn in tests: fn()
    print(f'{len(tests)} function tests passed')
