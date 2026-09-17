from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/v7_ena_latency_ab.py"
SSM = ROOT / "ops/v7_london_ena_ab_ssm.py"

spec = importlib.util.spec_from_file_location("v7_ena_latency_ab", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_ena_coalescing() -> None:
    value = module.parse_coalesce("""Coalesce parameters for eth0:\nAdaptive RX: off  TX: off\nrx-usecs: 20\ntx-usecs: 64\n""")
    assert value == {
        "adaptive_rx": "off", "adaptive_tx": "off",
        "rx_usecs": 20, "tx_usecs": 64,
    }


def test_role_selection_reserves_decision_from_feed_irqs() -> None:
    roles = module.select_roles(list(range(8)))
    assert roles["decision"] == 4
    assert roles["feeds"] == [5, 6, 7]
    assert roles["decision"] not in roles["feeds"]


def _probe(total_p50: int, total_p99: int, total_p999: int,
           first_byte_p99: int, failed: int = 0) -> dict:
    return {
        "failed_samples": failed,
        "timings_ns": {
            "total": {"p50": total_p50, "p99": total_p99, "p99_9": total_p999},
            "first_byte": {"p99": first_byte_p99},
        },
    }


def test_delta_is_candidate_minus_paired_baseline() -> None:
    baseline = _probe(100, 500, 900, 400)
    candidate = _probe(90, 420, 700, 350)
    value = module.delta(candidate, baseline)
    assert value["total_p50_ns"] == -10
    assert value["total_p99_ns"] == -80
    assert value["total_p99_9_ns"] == -200
    assert value["first_byte_p99_ns"] == -50
    assert value["failed_samples_delta"] == 0


def test_profiles_are_interleaved_and_rollback_verified() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for name in ("busy_poll_50", "interrupt_moderation_zero",
                 "irq_napi_feed_affinity", "combined"):
        assert name in source
    assert "finally:" in source
    assert "restore(baseline_state)" in source
    assert 'result["restored_exactly"] = restored' in source
    assert 'Path("/proc/sys/net/core/busy_poll").read_text()' in source
    assert "sysctl -w" not in source
    assert 'role_cpus()["decision"]' not in source


def test_ssm_wrapper_is_three_zone_paper_only() -> None:
    source = SSM.read_text(encoding="utf-8")
    assert "for zone, instance_id in instances.items()" in source
    assert "polymarket-v7-paper.service" in source
    assert 'v["authenticated_execution"] is False' in source
    assert 'v["real_order_submission"] is False' in source
    assert "v['persistent_tuning'] is False" in source
    assert "v['restored_exactly'] is True" in source


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for _, test in tests:
        test()
    print(f"{len(tests)} function tests passed")
