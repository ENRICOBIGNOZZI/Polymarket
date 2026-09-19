from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'src/v7_maker_fillability_observer.cpp'


def test_token_local_lineage_never_drives_global_observer_reload():
    source = SOURCE.read_text()
    loop = source.split('int main(int argc, char** argv)', 1)[1]
    assert 'observer.root_lineage_recovery_requested()' in loop
    assert '&& observer.lineage_recovery_requested()' not in loop
    assert 'root_lineage_recovery_requested_.store(true' in source
    token_local = source.split('event.kind == MarketWsEventKind::LineageInvalidated', 1)[1].split('if (event.kind == MarketWsEventKind::Trade', 1)[0]
    assert 'lineage_recovery_requested_.exchange(true' in token_local
    assert 'root_lineage_recovery_requested_.store(true' not in token_local


def test_transport_reconnect_forces_authoritative_cold_bootstrap():
    source = SOURCE.read_text()
    reconnect = source.split('void on_reconnect()', 1)[1].split('void start()', 1)[0]
    assert 'root_lineage_recovery_requested_.store(true' in reconnect
    assert 'lineage_recovery_requested_.exchange(true' in reconnect
    loop = source.split('int main(int argc, char** argv)', 1)[1]
    assert 'observer.root_lineage_recovery_requested()' in loop
