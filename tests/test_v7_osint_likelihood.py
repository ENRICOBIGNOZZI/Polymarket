import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from v7_function_test_support import function_test_loader
import v7_osint_engine as o
import v7_osint_likelihood as likelihood


def observations():
    rows = []
    for index in range(48):
        outcome = index % 2
        rows.append(o.LikelihoodObservation(
            "REGULATORY_APPROVAL", f"root-{index}", 1000 + index * 100,
            1 if outcome else -1, outcome, .5,
        ))
    return rows


def test_registry_is_frozen_exact_sha_and_never_authorizes_execution():
    value = likelihood.build_registry(
        observations(), model_sha="a" * 40, cutoff_ms=3300,
        embargo_ms=100, minimum_oos_events=20,
    )
    assert value["model_sha"] == "a" * 40
    assert value["independent_sample_unit"] == "root_lineage_id"
    assert value["automatic_promotion"] is False
    assert value["event_families"][0]["status"] == "ENGINEERING_VALIDATED"
    assert value["event_families"][0]["execution_authority"] is False


def test_oos_lineage_overlap_fails_closed():
    train = observations()[:24]
    overlap = list(observations()[24:])
    overlap[0] = o.LikelihoodObservation(
        "REGULATORY_APPROVAL", train[0].root_lineage_id, 5000, 1, 1, .5
    )
    try:
        o.fit_likelihood_model(
            "REGULATORY_APPROVAL", train, trained_until_ms=4000,
            oos_rows=overlap, minimum_oos_events=1,
        )
    except o.OsintError as exc:
        assert str(exc) == "likelihood_oos_lineage_overlap"
    else:
        raise AssertionError("overlapping root lineage must not enter OOS")


load_tests = function_test_loader(globals())

if __name__ == "__main__":
    unittest.main()
