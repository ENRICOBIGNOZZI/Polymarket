import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_session_registry as registry  # noqa: E402


SHA = "a" * 40


def valid() -> dict:
    return {"schema": registry.SCHEMA, "model_sha": SHA, "wallet_id_hash": "b" * 64,
            "registry_evidence_hash": "c" * 64,
            "sessions": [{"session_key_id_hash": "d" * 64, "activated_at_ms": 1,
                          "retired_at_ms": None, "registration_evidence_hash": "e" * 64}]}


class SessionRegistryTests(unittest.TestCase):
    def test_redacted_registry_accepts_only_sorted_hashed_sessions(self) -> None:
        self.assertEqual(registry.validate(valid(), expected_model_sha=SHA)["model_sha"], SHA)
        value = valid(); value["sessions"][0]["session_key_id_hash"] = "not-a-hash"
        with self.assertRaisesRegex(registry.SessionRegistryError, "session_identity"):
            registry.validate(value)

    def test_missing_or_mismatched_model_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(registry.SessionRegistryError, "registry_shape"):
                registry.load(path, expected_model_sha=SHA)
        with self.assertRaisesRegex(registry.SessionRegistryError, "registry_model_sha"):
            registry.validate(valid(), expected_model_sha="f" * 40)


if __name__ == "__main__":
    unittest.main()
