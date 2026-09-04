from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from v7_binary_tape_retention import (
    RAW_RECORD_HEADER,
    TAPE_HEADER_BYTES,
    archive_closed_binary_tapes,
    validate_closed_tape,
)

SHA = "c" * 40


def header(
    *,
    magic: bytes,
    record_bytes: int,
    sha: str = SHA,
    source: str = "binance-spot",
) -> bytes:
    value = bytearray(TAPE_HEADER_BYTES)
    value[0:8] = magic
    value[8:12] = (3).to_bytes(4, "little")
    value[12:16] = int(record_bytes).to_bytes(4, "little")
    value[16:24] = int(1_000_000).to_bytes(8, "little", signed=True)
    value[24:65] = sha.encode() + b"\0"
    value[65:130] = b"paper-v7\0" + bytes(65 - 9)
    value[130:195] = b"session-1\0" + bytes(65 - 10)
    encoded_source = source.encode() + b"\0"
    value[195:228] = encoded_source + bytes(33 - len(encoded_source))
    return bytes(value)


def normalized_record(sequence: int) -> bytes:
    record = bytearray(544)
    record[0:8] = sequence.to_bytes(8, "little")
    record[8:16] = (1000 + sequence).to_bytes(8, "little", signed=True)
    record[16:24] = (7).to_bytes(8, "little")
    record[24:26] = (2).to_bytes(2, "little")
    record[28:32] = (16).to_bytes(4, "little")
    record[32:48] = bytes([sequence]) * 16
    return bytes(record)


def policy() -> dict:
    return {
        "enabled": True,
        "patterns": [
            "external_fair/raw/*.bin",
            "external_fair/normalized_events/*.bin",
        ],
        "archive_directory": "archive/binary_tapes",
        "minimum_closed_age_seconds": 5,
        "allowed_schema_versions": [1, 2, 3],
        "maximum_raw_payload_bytes": 2 * 1024 * 1024,
    }


class BinaryTapeRetentionTests(unittest.TestCase):
    def test_normalized_segment_is_verified_archived_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "external_fair/normalized_events/"
                "binance-spot.999999.segment-000000.bin"
            )
            source.parent.mkdir(parents=True)
            payload = (
                header(magic=b"PMV7TAPE", record_bytes=544)
                + normalized_record(1)
                + normalized_record(2)
            )
            source.write_bytes(payload)
            os.utime(source, (1, 1))
            result = archive_closed_binary_tapes(
                root, policy(), SHA, now=100, dry_run=False
            )
            self.assertTrue(result["complete"])
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(len(result["archived"]), 1)
            self.assertFalse(source.exists())
            archive = root / result["archived"][0]["archive_relative"]
            self.assertEqual(gzip.open(archive, "rb").read(), payload)
            manifest = json.loads(
                (root / "archive/binary_tapes/manifest.json").read_text()
            )
            self.assertEqual(manifest["model_sha"], SHA)
            self.assertEqual(manifest["segments"][0]["records"], 2)
            self.assertGreater(result["reclaimed_bytes"], 0)

    def test_raw_segment_is_verified_and_archived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "external_fair/raw/bybit-spot.999998.segment-000001.bin"
            )
            source.parent.mkdir(parents=True)
            raw = bytearray(
                header(
                    magic=b"PMV7RAW!", record_bytes=0, source="bybit-spot"
                )
            )
            for sequence, body in ((1, b"abc"), (2, b"defgh")):
                raw += RAW_RECORD_HEADER.pack(
                    sequence,
                    1,
                    100 + sequence,
                    1000 + sequence,
                    3,
                    len(body),
                )
                raw += body
            payload = bytes(raw)
            source.write_bytes(payload)
            os.utime(source, (1, 1))
            validation = validate_closed_tape(source, SHA)
            self.assertEqual(validation.records, 2)
            result = archive_closed_binary_tapes(
                root, policy(), SHA, now=100, dry_run=False
            )
            self.assertTrue(result["complete"])
            archive = root / result["archived"][0]["archive_relative"]
            self.assertEqual(gzip.open(archive, "rb").read(), payload)
            self.assertFalse(source.exists())

    def test_active_open_and_live_legacy_writer_are_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "external_fair/raw"
            raw.mkdir(parents=True)
            active = raw / "binance-spot.123.segment-000000.bin.open"
            active.write_bytes(b"active")
            legacy = raw / f"binance-spot.{os.getpid()}.bin"
            legacy.write_bytes(header(magic=b"PMV7RAW!", record_bytes=0))
            os.utime(active, (1, 1))
            os.utime(legacy, (1, 1))
            result = archive_closed_binary_tapes(
                root, policy(), SHA, now=100, dry_run=False
            )
            self.assertEqual(result["candidate_count"], 0)
            self.assertTrue(active.exists())
            self.assertTrue(legacy.exists())
            reasons = {row["reason"] for row in result["skipped"]}
            self.assertIn("legacy_writer_pid_alive", reasons)

    def test_corrupt_or_wrong_sha_segment_is_preserved_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "external_fair/normalized_events/"
                "binance-spot.999997.segment-000000.bin"
            )
            source.parent.mkdir(parents=True)
            source.write_bytes(
                header(
                    magic=b"PMV7TAPE", record_bytes=544, sha="d" * 40
                )
                + b"broken"
            )
            os.utime(source, (1, 1))
            result = archive_closed_binary_tapes(
                root, policy(), SHA, now=100, dry_run=False
            )
            self.assertFalse(result["complete"])
            self.assertEqual(len(result["failures"]), 1)
            self.assertTrue(source.exists())
            self.assertFalse(
                (root / "archive/binary_tapes/manifest.json").exists()
            )

    def test_dry_run_does_not_modify_source_or_create_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "external_fair/normalized_events/"
                "binance-spot.999996.segment-000000.bin"
            )
            source.parent.mkdir(parents=True)
            payload = (
                header(magic=b"PMV7TAPE", record_bytes=544)
                + normalized_record(1)
            )
            source.write_bytes(payload)
            os.utime(source, (1, 1))
            result = archive_closed_binary_tapes(
                root, policy(), SHA, now=100, dry_run=True
            )
            self.assertTrue(result["complete"])
            self.assertEqual(len(result["archived"]), 1)
            self.assertTrue(source.exists())
            self.assertFalse((root / "archive/binary_tapes").exists())


if __name__ == "__main__":
    unittest.main()
