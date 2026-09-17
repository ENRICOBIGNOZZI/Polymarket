from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"

PROGRAM = r'''
#include "pm/v7_secp256k1_identity.hpp"
#include <array>
#include <cstdint>
using namespace pm::v7::clob_signing;
int main() {
    std::array<std::uint8_t, 32> key{};
    key[31] = 1;
    EthereumAddress20 address{};
    if (!derive_ethereum_address(key, address)) return 2;
    const std::array<std::uint8_t, 20> expected{
        0x7e,0x5f,0x45,0x52,0x09,0x1a,0x69,0x12,0x5d,0x5d,
        0xfc,0xb7,0xb8,0xc2,0x65,0x90,0x29,0x39,0x5b,0xdf};
    if (address != expected) return 3;
    if (!secret_key_matches_ethereum_address(
            key, "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf")) return 4;
    if (!secret_key_matches_ethereum_address(
            key, "7e5f4552091a69125d5dfcb7b8c2659029395bdf")) return 5;
    if (secret_key_matches_ethereum_address(
            key, "0x1111111111111111111111111111111111111111")) return 6;
    if (secret_key_matches_ethereum_address(key, "0x1234")) return 7;
    std::array<std::uint8_t, 32> zero{};
    if (derive_ethereum_address(zero, address)) return 8;
    if (secret_key_matches_ethereum_address(zero,
            "0x0000000000000000000000000000000000000000")) return 9;
    return 0;
}
'''


class SecpIdentityTest(unittest.TestCase):
    def test_secret_key_is_bound_to_expected_ethereum_address(self) -> None:
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        available = subprocess.run(
            ["pkg-config", "--exists", "libsecp256k1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not available:
            self.skipTest("libsecp256k1 candidate dependency not installed")
        flags = subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "libsecp256k1"], text=True
        ).strip()
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "identity-test"
            subprocess.run(
                [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
                 f"-I{ROOT / 'include'}",
                 str(ROOT / "src/v7_secp256k1_identity.cpp"),
                 str(ROOT / "src/v7_clob_eip712.cpp"),
                 str(ROOT / "src/v7_keccak_fast.cpp"),
                 "-x", "c++", "-", *shlex.split(flags), "-o", str(binary)],
                input=PROGRAM, text=True, check=True, capture_output=True,
            )
            subprocess.run([str(binary)], check=True)
        self.assertEqual(
            EXPECTED, "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
        )


if __name__ == "__main__":
    unittest.main()
