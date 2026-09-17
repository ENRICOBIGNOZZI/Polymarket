from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_clob_signing_contract.hpp"
#include <cstdint>
using namespace pm::v7::clob_signing;
int main() {
    static_assert(static_cast<std::uint8_t>(SignatureMode::Eoa) == 0);
    static_assert(static_cast<std::uint8_t>(SignatureMode::PolyProxy) == 1);
    static_assert(static_cast<std::uint8_t>(SignatureMode::PolyGnosisSafe) == 2);
    static_assert(static_cast<std::uint8_t>(SignatureMode::Poly1271) == 3);

    constexpr auto maker = "0x1111111111111111111111111111111111111111";
    constexpr auto key = "0x2222222222222222222222222222222222222222";
    SignatureMode mode{};
    if (!parse_signature_mode("POLY_PROXY", mode) || mode != SignatureMode::PolyProxy) return 2;
    if (parse_signature_mode("UNSET", mode)) return 3;
    for (auto m : {SignatureMode::Eoa, SignatureMode::PolyProxy, SignatureMode::PolyGnosisSafe}) {
        const auto contract = build_signing_contract(m, maker, key);
        if (!contract.valid || contract.order_signer != key || contract.requires_poly1271_wrap) return 4;
    }
    const auto wrapped = build_signing_contract(SignatureMode::Poly1271, maker, key);
    if (!wrapped.valid || wrapped.order_signer != maker || !wrapped.requires_poly1271_wrap) return 5;
    if (build_signing_contract(SignatureMode::Eoa, "0x1234", key).valid) return 6;
    if (build_signing_contract(SignatureMode::Eoa, maker, "not-an-address").valid) return 7;
    return 0;
}
'''


def test_exchange_v2_signing_contract_matches_official_modes() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "signing-contract-test"
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", str(ROOT / "src/v7_clob_signing_contract.cpp"),
            "-x", "c++", "-", "-o", str(binary),
        ], input=PROGRAM, text=True, check=True, capture_output=True)
        subprocess.run([str(binary)], check=True)
