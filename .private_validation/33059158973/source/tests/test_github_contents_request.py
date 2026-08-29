#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_github_contents_request import build_payload


class GithubContentsRequestTest(unittest.TestCase):
    def test_large_file_is_encoded_in_request_body(self) -> None:
        raw = (b"forward-maker-evidence\n" * 20000) + bytes(range(256))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_bytes(raw)
            payload = build_payload(path, "publish maker evidence", "telemetry", "a" * 40)

        self.assertEqual(payload["message"], "publish maker evidence")
        self.assertEqual(payload["branch"], "telemetry")
        self.assertEqual(payload["sha"], "a" * 40)
        self.assertEqual(base64.b64decode(payload["content"]), raw)
        self.assertGreater(len(payload["content"]), 128_000)
        json.dumps(payload)

    def test_sha_is_omitted_for_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.txt"
            path.write_text("ok", encoding="utf-8")
            payload = build_payload(path, "create", "telemetry")
        self.assertNotIn("sha", payload)


if __name__ == "__main__":
    unittest.main()
