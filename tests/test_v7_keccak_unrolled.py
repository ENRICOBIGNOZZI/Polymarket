from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r"""
#include "pm/v7_clob_eip712.hpp"
#include "pm/v7_keccak_fast.hpp"

#include <array>
#include <cstdint>
#include <string_view>
#include <vector>

using namespace pm::v7::clob_eip712;

bool same_hex(const Hash32& hash, std::string_view expected) {
    std::array<char, 64> text{};
    return expected.size() == text.size() && hash32_hex(hash, text)
        && std::string_view(text.data(), text.size()) == expected;
}

int main() {
    if (!same_hex(keccak256_unrolled({}),
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")) return 2;
    constexpr std::string_view abc = "abc";
    const auto abc_hash = keccak256_unrolled(std::span<const std::uint8_t>(
        reinterpret_cast<const std::uint8_t*>(abc.data()), abc.size()));
    if (!same_hex(abc_hash,
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")) return 3;

    std::uint64_t state = 0x9e3779b97f4a7c15ULL;
    for (std::size_t length = 0; length <= 1024; ++length) {
        std::vector<std::uint8_t> input(length);
        for (auto& byte : input) {
            state ^= state << 13U;
            state ^= state >> 7U;
            state ^= state << 17U;
            byte = static_cast<std::uint8_t>(state);
        }
        const auto span = std::span<const std::uint8_t>(input.data(), input.size());
        if (keccak256_unrolled(span) != keccak256(span)) return 4;
    }
    return 0;
}
"""


def test_unrolled_keccak_matches_verified_reference() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        source = path / "main.cpp"
        binary = path / "keccak-parity"
        source.write_text(PROGRAM)
        subprocess.run(
            [
                compiler,
                "-std=c++20",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                f"-I{ROOT / 'include'}",
                str(ROOT / "src/v7_clob_eip712.cpp"),
                str(ROOT / "src/v7_keccak_fast.cpp"),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=20)
