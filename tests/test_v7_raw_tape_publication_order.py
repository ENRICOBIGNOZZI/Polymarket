"""Compile the actual recorder and verify serialized arrival order.

The mutation check restores the former size-priority writer loop. It must fail
on sequence ordering, not compilation or a timeout. No network or trading.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
#include "pm/v7_external_tape.hpp"
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>
using namespace pm::v7::external_fair;
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
int main(int argc, char** argv) {
    try {
        require(argc == 2, "path required");
        std::filesystem::path path(argv[1]);
        std::vector<std::pair<std::uint64_t, std::string>> expected;
        std::uint64_t sequence = 0;
        {
            ExternalRawTapeRecorder recorder(path, std::string(40, 'a'),
                "mixed-size-test", "single-producer", "coinbase-spot", 1000);
            for (int batch = 0; batch < 64; ++batch) {
                const std::string payloads[] = {
                    std::string(kExternalRawTapePayloadBytes + 17, 'L'),
                    "small-" + std::to_string(batch),
                    std::string(kExternalRawTapePayloadBytes * 2 + 3, 'R'),
                    "tail-" + std::to_string(batch),
                };
                for (const auto& payload : payloads) {
                    ++sequence;
                    require(recorder.try_record_raw(VenueId::CoinbaseSpot, 1,
                        100 + sequence, 1000 + sequence, payload), "unexpected drop");
                    expected.emplace_back(sequence, payload);
                }
                const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
                while (recorder.snapshot().written != expected.size()) {
                    require(std::chrono::steady_clock::now() < deadline, "writer timeout");
                    std::this_thread::sleep_for(std::chrono::microseconds(100));
                }
                if (batch == 31) {
                    ++sequence;
                    require(!recorder.try_record_raw(VenueId::CoinbaseSpot, 1,
                        100 + sequence, 1000 + sequence,
                        std::string(kExternalLargeRawTapePayloadBytes + 1, 'X')),
                        "oversized frame accepted");
                }
            }
            const auto status = recorder.snapshot();
            require(status.accepted == expected.size(), "accepted count mismatch");
            require(status.written == expected.size(), "written count mismatch");
            require(status.dropped == 1 && status.dropped_payload_too_large == 1,
                    "loss accounting mismatch");
            require(status.dropped_queue_full == 0, "unexpected queue overflow");
            require(status.evidence_valid == 0 && status.writer_healthy == 1,
                    "oversized gap must invalidate evidence without killing writer");
        }
        std::ifstream input(path, std::ios::binary);
        TapeSessionHeader session;
        require(bool(input.read(reinterpret_cast<char*>(&session), sizeof(session))), "header missing");
        require(std::string(session.magic.data(), 8) == "PMV7RAW!", "wrong magic");
        for (const auto& [seq, payload] : expected) {
            RawTapeDiskRecordHeader record;
            require(bool(input.read(reinterpret_cast<char*>(&record), sizeof(record))), "record missing");
            require(record.tape_sequence == seq, "sequence mismatch");
            require(record.receive_monotonic_ns == 100 + seq, "receive time mismatch");
            require(record.receive_wall_ns == 1000 + seq, "wall time mismatch");
            require(record.venue == VenueId::CoinbaseSpot && record.connection_epoch == 1,
                    "source identity mismatch");
            require(record.payload_size == payload.size(), "payload size mismatch");
            std::string actual(record.payload_size, '\0');
            require(bool(input.read(actual.data(), actual.size())), "payload missing");
            require(actual == payload, "payload mismatch");
        }
        require(input.peek() == std::char_traits<char>::eof(), "unexpected extra record");
        std::cout << "verified 256 mixed-size records; rejected gap preserved\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
'''

OLD_LOOP = '''    void writer_loop() noexcept {
        while (!stop_requested.load(std::memory_order_acquire) || queued() != 0) {
            bool progressed = false;
            while (queue.try_pop(writer_record)) {
                progressed = true;
                if (!write_record(writer_record)) return;
            }
            while (large_queue->try_pop(*large_writer_record)) {
                progressed = true;
                if (!write_record(*large_writer_record)) return;
            }
            if (!progressed) std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
        output.flush();
    }
'''


class RawTapePublicationOrderTest(unittest.TestCase):
    def compile_and_run(self, source: str) -> subprocess.CompletedProcess[str]:
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler, "C++20 compiler required for recorder regression")
        with tempfile.TemporaryDirectory(prefix="v7-raw-order-") as directory:
            root = Path(directory)
            (root / "recorder.cpp").write_text(source, encoding="utf-8")
            (root / "harness.cpp").write_text(HARNESS, encoding="utf-8")
            build = subprocess.run([
                compiler, "-std=c++20", "-O2", "-pthread", "-I", str(ROOT / "include"),
                str(root / "recorder.cpp"), str(root / "harness.cpp"),
                "-o", str(root / "check"),
            ], capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            return subprocess.run([str(root / "check"), str(root / "tape.bin")],
                                  capture_output=True, text=True, timeout=30)

    def test_mixed_size_frames_preserve_publication_order_and_gap(self) -> None:
        source = (ROOT / "src/v7_external_tape.cpp").read_text(encoding="utf-8")
        result = self.compile_and_run(source)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verified 256 mixed-size records", result.stdout)

    def test_size_priority_writer_regression_is_detected(self) -> None:
        source = (ROOT / "src/v7_external_tape.cpp").read_text(encoding="utf-8")
        start = source.index("    void writer_loop() noexcept {", source.index("struct ExternalRawTapeRecorder::Impl"))
        end = source.index("};\n\nExternalRawTapeRecorder::ExternalRawTapeRecorder", start)
        mutant = source[:start] + OLD_LOOP + source[end:]
        result = self.compile_and_run(mutant)
        self.assertNotEqual(result.returncode, 0, "regression harness missed size-priority reordering")
        self.assertIn("sequence mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
