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
    assert 'module_imports' in source and 'local_closure' in source
    assert 'verify_git_tree(root,sha,files)' in source
    assert 'London runtime source checkout is dirty' in source
    assert "git','-C',str(root),'show'" in source
    assert "'source_tree_verified':True" in source


def test_london_retention_dependency_is_runtime_only():
    entry=(ROOT/'monitoring/v7_london_buffer_retention.py').read_text()
    runtime=(ROOT/'monitoring/v7_closed_tape_retention.py').read_text()
    assert 'from v7_closed_tape_retention import compress_closed_cutover_tapes' in entry
    assert 'v7_retention' not in entry
    tree=ast.parse(runtime)
    local_imports={
        node.module.split('.')[0]
        for node in ast.walk(tree)
        if isinstance(node,ast.ImportFrom) and node.module
    }
    local_imports.update(
        alias.name.split('.')[0]
        for node in ast.walk(tree) if isinstance(node,ast.Import)
        for alias in node.names
    )
    assert not ({'v7_lossless_data_compaction','v7_permanent_evidence','v7_aggregate_retention'} & local_imports)
    assert 'research' not in local_imports
