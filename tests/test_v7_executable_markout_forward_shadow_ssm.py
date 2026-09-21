import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))
SPEC = importlib.util.spec_from_file_location(
    "v7_executable_markout_forward_shadow_ssm",
    ROOT / "ops/v7_executable_markout_forward_shadow_ssm.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def request():
    return {
        "schema": "polymarket_v7_executable_markout_forward_shadow_request_v1",
        "version": 1,
        "request_id": "markout-shadow-20260921",
        "instance_id": "i-0fba2bac9fdc5cbeb",
        "artifact_path": "docs/research/historical-walk-forward-v2-2026-09-21/full_window_repricing_models.json",
        "artifact_sha256": "a" * 64,
        "source_code_sha": "b" * 40,
        "duration_seconds": 7200,
        "poll_ms": 10,
        "maximum_inference_age_ms": 100,
        "receipt_path": "docs/research/executable-markout-forward-shadow-launch-2026-09-21.json",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }


def write(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    return path


def test_request_accepts_only_bounded_zero_authority_shadow(tmp_path: Path):
    value = MODULE.load_request(write(tmp_path, request()))
    assert value["duration_seconds"] == 7200
    assert value["maximum_inference_age_ms"] == 100


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("paper_only", False),
        ("authenticated_execution", True),
        ("real_order_submission", True),
        ("real_capital_at_risk", True),
        ("duration_seconds", 20000),
        ("maximum_inference_age_ms", 500),
        ("artifact_sha256", "bad"),
        ("source_code_sha", "bad"),
        ("artifact_path", "config/other.json"),
    ],
)
def test_request_rejects_authority_or_identity_drift(tmp_path: Path, field: str, value):
    item = request()
    item[field] = value
    with pytest.raises(ValueError):
        MODULE.load_request(write(tmp_path, item))
