import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))
sys.path.insert(0, str(ROOT / "tests"))
from v7_function_test_support import function_test_loader
import exporter_v7 as exporter


def test_osint_source_health_is_visible_without_becoming_execution_health(tmp_path):
    status = {
        "schema": "polymarket_v7_osint_collector_status_v1",
        "enabled_sources": 2,
        "healthy_sources": 1,
        "new_events": 3,
        "sources": [
            {"source_id": "official_a", "healthy": True, "new_events": 3},
            {"source_id": "official_b", "healthy": False, "new_events": 0},
        ],
    }
    path = tmp_path / "osint" / "status.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(status))
    open_path = tmp_path / "market_open" / "status.json"
    open_path.parent.mkdir(parents=True)
    open_path.write_text(json.dumps({
        "tracked_markets": 500, "new_markets": 2, "emitted_milestones": 7,
        "semantic_verified_markets": 0,
    }))
    snapshot = exporter.collect_snapshot(tmp_path, ROOT, now=1)
    metrics = exporter.render_prometheus(snapshot)
    assert "polymarket_v7_osint_enabled_sources 2" in metrics
    assert "polymarket_v7_osint_healthy_sources 1" in metrics
    assert 'polymarket_v7_osint_source_healthy{source="official_b"} 0' in metrics
    assert snapshot["strategies"]["osint"]["paper_eligible"] is False
    assert "polymarket_v7_market_open_new_markets 2" in metrics
    assert "polymarket_v7_market_open_semantic_verified 0" in metrics
    assert snapshot["strategies"]["market_open"]["paper_eligible"] is False


load_tests = function_test_loader(globals())

if __name__ == "__main__":
    unittest.main()
