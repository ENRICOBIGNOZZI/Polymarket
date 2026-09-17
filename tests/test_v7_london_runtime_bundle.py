from __future__ import annotations
import ast,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_bundle_manifest_is_minimal_and_forbids_research():
    m=json.loads((ROOT/'deploy/london/runtime_manifest.json').read_text())
    assert m['paper_only'] is True and m['authenticated_execution'] is False and m['real_order_submission'] is False
    assert 'polymarket_v7_external_venue_runtime' in m['binaries']
    assert all(not x.startswith('research/') for x in m['support_files']+m['python_entrypoints'])
    assert 'v7_maker_durable_learning.py' in m['forbidden_path_fragments']
    assert 'tests/' in m['forbidden_path_fragments']

def test_bundle_builder_has_final_tree_forbidden_gate():
    source=(ROOT/'ops/build_london_runtime_bundle.py').read_text()
    ast.parse(source)
    assert 'forbidden London files' in source
    assert 'local_imports' in source and 'closure' in source
