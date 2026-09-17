import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_ena_host_ab", ROOT / "ops/v7_ena_host_ab.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_profiles_never_enable_busy_poll():
    names = [p["name"] for p in MODULE.PROFILES]
    assert names == ["interrupt_moderation_zero", "irq_napi_feed_affinity", "combined"]
    assert all("busy_poll_us" not in p for p in MODULE.PROFILES)


def test_load_roles_uses_decision_plus_three_feeds():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plan.json"
        path.write_text(json.dumps({
            "schema": "polymarket_v7_runtime_resource_plan_v2",
            "outer_cpuset_contract_satisfied": True,
            "hot_path_cpus": [4, 5, 6, 7],
        }))
        roles = MODULE.load_roles(path)
        assert roles["decision"] == 4
        assert roles["feeds"] == [5, 6, 7]


def test_coalesce_parser():
    raw = "Adaptive RX: on  TX: off\nrx-usecs: 20\ntx-usecs: 30\n"
    assert MODULE.parse_coalesce(raw) == {
        "adaptive_rx": "on", "adaptive_tx": "off",
        "rx_usecs": 20, "tx_usecs": 30,
    }


def test_delta_prefers_lower_tail_and_no_failure_growth():
    def sample(p50, p99, p999, first, failed=0):
        return {"timings_ns": {
            "total": {"p50": p50, "p99": p99, "p99_9": p999},
            "first_byte": {"p99": first}}, "failed_samples": failed}
    value = MODULE.delta(sample(90, 180, 250, 140), sample(100, 200, 300, 160))
    assert value["total_p99_ns"] == -20
    assert value["total_p99_9_ns"] == -50
    assert value["failed_samples_delta"] == 0
