from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r"""
#include "pm/v7_clob_eip712.hpp"
#include <array>
#include <charconv>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <limits>

using namespace pm::v7::clob_eip712;

int main() {
    ExchangeV2DomainView domain{80002, "0xE111180000d2663C0091e4f400237545B87B996B"};
    ExchangeV2OrderView generic{
        "479249096354",
        "0x1111111111111111111111111111111111111111",
        "0x1111111111111111111111111111111111111111",
        "1234", "100000000", "50000000", 0, 3, "1710000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000"
    };
    ExchangeV2PreparedStaticView fixed{
        generic.maker, generic.signer, generic.token_id_decimal,
        generic.side, generic.signature_type, generic.metadata_hex, generic.builder_hex
    };
    ExchangeV2OrderHasher general(domain);
    ExchangeV2PreparedOrderHasher prepared(domain, fixed);
    if (!general.valid() || !prepared.valid()) return 2;
    Hash32 a{}, b{}, generic_struct{}, prepared_struct{};
    if (!general.digest(generic, a)) return 3;
    if (!prepared.digest_u64(479249096354ULL, 100000000ULL, 50000000ULL,
                             1710000000000ULL, b)) return 4;
    if (a != b) return 5;
    if (!exchange_v2_order_struct_hash(generic, generic_struct)) return 8;
    if (!prepared.struct_hash_u64(479249096354ULL, 100000000ULL, 50000000ULL,
                                  1710000000000ULL, prepared_struct)) return 9;
    if (generic_struct != prepared_struct) return 10;

    struct DynamicCase {
        std::uint64_t salt;
        std::uint64_t maker;
        std::uint64_t taker;
        std::uint64_t timestamp;
    };
    constexpr std::array<DynamicCase, 3> cases{{
        {0ULL, 0ULL, 0ULL, 0ULL},
        {1ULL, 1ULL, 1ULL, 1ULL},
        {std::numeric_limits<std::uint64_t>::max(),
         std::numeric_limits<std::uint64_t>::max() - 1ULL,
         std::numeric_limits<std::uint64_t>::max() - 2ULL,
         std::numeric_limits<std::uint64_t>::max() - 3ULL},
    }};
    for (const auto& value : cases) {
        std::array<char, 32> salt{}, maker{}, taker{}, timestamp{};
        const auto decimal = [](std::uint64_t input, auto& buffer) -> std::string_view {
            const auto result = std::to_chars(buffer.data(),
                                              buffer.data() + buffer.size(), input);
            if (result.ec != std::errc{}) return {};
            return {buffer.data(), static_cast<std::size_t>(result.ptr - buffer.data())};
        };
        generic.salt_decimal = decimal(value.salt, salt);
        generic.maker_amount_decimal = decimal(value.maker, maker);
        generic.taker_amount_decimal = decimal(value.taker, taker);
        generic.timestamp_decimal = decimal(value.timestamp, timestamp);
        if (generic.salt_decimal.empty() || generic.maker_amount_decimal.empty()
            || generic.taker_amount_decimal.empty() || generic.timestamp_decimal.empty()) return 11;
        if (!exchange_v2_order_struct_hash(generic, generic_struct)) return 12;
        if (!prepared.struct_hash_u64(value.salt, value.maker, value.taker,
                                      value.timestamp, prepared_struct)) return 13;
        if (generic_struct != prepared_struct) return 14;
    }

    std::array<char, 64> out{};
    if (!hash32_hex(b, out)) return 6;
    std::printf("%.*s\n", 64, out.data());

    ExchangeV2PreparedStaticView bad = fixed;
    bad.token_id_decimal = "not-a-number";
    ExchangeV2PreparedOrderHasher invalid(domain, bad);
    if (invalid.valid()) return 7;
    return 0;
}
"""

EXPECTED = "2afca94626db91b2d556c351584a4cd93e13da32e377dc48c7983e43afa5ab47"


def test_prepared_eip712_matches_general_and_official_vector() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        main = path / "main.cpp"
        binary = path / "prepared-test"
        main.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", str(ROOT / "src/v7_clob_eip712.cpp"),
            str(ROOT / "src/v7_keccak_fast.cpp"),
            str(main), "-o", str(binary),
        ], check=True, capture_output=True, text=True)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
        assert result.stdout.strip() == EXPECTED
