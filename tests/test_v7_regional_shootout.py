#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/v7_regional_shootout.py"
POLICY = ROOT / "config/v7_latency_slo.json"
SHA = "1" * 40


def probe(region: str, p99_9: int, p99: int, *, sha: str = SHA) -> dict:
    dist = {"p50": 1_000_000, "p90": 2_000_000, "p95": 3_000_000,
            "p99": p99, "p99_9": p99_9, "max": p99_9 + 1_000_000}
    return {
        "schema": "polymarket_v7_regional_latency_probe_v1",
        "endpoint": "https://clob.polymarket.com/time",
        "region": region,
        "exact_code_sha": sha,
        "started_wall_ms": 1_000,
        "finished_wall_ms": 86_402_000,
        "samples": 1_010,
        "successful_samples": 1_005,
        "failed_samples": 5,
        "reconnect_count": 2,
        "timings_ns": {name: dict(dist) for name in
                       ("dns", "tcp_connect", "tls_connect", "first_byte", "total")},
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "measures_order_or_cancel_ack": False,
    }


def run(rows: list[dict], candidates: list[str]) -> tuple[subprocess.CompletedProcess[str], dict]:
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, row in enumerate(rows):
            path = Path(tmp) / f"probe-{i}.json"
            path.write_text(json.dumps(row), encoding="utf-8")
            paths.append(path)
        command = [sys.executable, str(SCRIPT), "--policy", str(POLICY)]
        for region in candidates:
            command += ["--candidate-region", region]
        for path in paths:
            command += ["--probe", str(path)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        output = json.loads(completed.stdout)
        return completed, output


def test_az_shootout_ranks_only_when_all_probes_are_healthy() -> None:
    candidates = ["eu-west-2a", "eu-west-2b", "eu-west-2c"]
    completed, output = run([
        probe("eu-west-2a", 9_000_000, 7_000_000),
        probe("eu-west-2b", 7_000_000, 6_000_000),
        probe("eu-west-2c", 8_000_000, 5_000_000),
    ], candidates)
    assert completed.returncode == 0, completed.stderr
    assert output["passed"] is True
    assert output["ranking"] == ["eu-west-2b", "eu-west-2c", "eu-west-2a"]
    assert output["selected_region"] == "eu-west-2b"
    assert output["authorizes_live_execution"] is False
    assert output["automatic_cutover"] is False


def test_mixed_sha_fails_closed() -> None:
    completed, output = run([
        probe("eu-west-2a", 7_000_000, 6_000_000),
        probe("eu-west-2b", 8_000_000, 6_000_000, sha="2" * 40),
    ], ["eu-west-2a", "eu-west-2b"])
    assert completed.returncode == 2
    assert output["passed"] is False
    assert "MIXED_OR_MISSING_SHA" in output["global_reasons"]
    assert output["selected_region"] is None


def test_missing_candidate_fails_closed() -> None:
    completed, output = run([
        probe("eu-west-2a", 7_000_000, 6_000_000),
    ], ["eu-west-2a", "eu-west-2b"])
    assert completed.returncode == 2
    assert output["missing_regions"] == ["eu-west-2b"]
    assert output["ranking"] == []


def test_malformed_percentiles_fail_closed() -> None:
    bad = probe("eu-west-2a", 7_000_000, 6_000_000)
    bad["timings_ns"]["total"]["p99_9"] = 5_000_000
    completed, output = run([bad], ["eu-west-2a"])
    assert completed.returncode == 2
    assert "MALFORMED_PERCENTILES:total" in output["results"][0]["reasons"]


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
