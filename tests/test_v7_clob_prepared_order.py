from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_prepared_order.hpp"
#include <array>
#include <string_view>
using namespace pm::v7::clob_wire;

bool same_case(std::string_view side, MarketOrderType type,
               std::string_view maker_amount, std::string_view salt,
               std::string_view signature, std::string_view taker_amount,
               std::string_view timestamp) {
    SignedMarketOrderView order{
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0",
        "0x1111111111111111111111111111111111111111",
        maker_amount,
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        salt,
        side,
        signature,
        3,
        "0x2222222222222222222222222222222222222222",
        taker_amount,
        timestamp,
        "71321045679252212594626385532706912750332728571942532289631379312455583992563"
    };
    PostMarketOrderView request{order, "owner-test-key", type};
    PreparedMarketOrderStaticView fixed{
        order.builder, order.expiration, order.maker, order.metadata, order.side,
        order.signature_type, order.signer, order.token_id, request.owner, request.order_type};
    PreparedMarketOrderJson prepared(fixed);
    if (!prepared.valid()) return false;
    MarketOrderDynamicView dynamic{
        order.maker_amount, order.salt_decimal, order.signature,
        order.taker_amount, order.timestamp_ms};
    std::array<char, 2048> reference{}, fast{};
    const auto a = serialize_post_market_order(request, reference);
    const auto b = prepared.serialize(dynamic, fast);
    return a != 0 && a == b
        && std::string_view(reference.data(), a) == std::string_view(fast.data(), b);
}

int main() {
    if (!same_case("BUY", MarketOrderType::FAK, "10000000", "479249096354",
                   "0xabababab", "19230800", "1789670000123")) return 2;
    if (!same_case("SELL", MarketOrderType::FOK, "19230800", "18446744073709551615",
                   "0xcdcdcdcdcdcd", "10000000", "1789670000999")) return 3;

    PreparedMarketOrderStaticView fixed{
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0", "0x1111111111111111111111111111111111111111",
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "BUY", 3, "0x2222222222222222222222222222222222222222",
        "1234", "owner-test-key", MarketOrderType::FAK};
    PreparedMarketOrderJson prepared(fixed);
    if (!prepared.valid()) return 4;
    std::array<char, 2048> out{};
    if (prepared.serialize({"1x", "2", "0xab", "3", "4"}, out) != 0) return 5;
    if (prepared.serialize({"1", "2", "bad\"sig", "3", "4"}, out) != 0) return 6;
    std::array<char, 16> small{};
    if (prepared.serialize({"1", "2", "0xab", "3", "4"}, small) != 0) return 7;
    fixed.side = "BOTH";
    if (PreparedMarketOrderJson(fixed).valid()) return 8;
    fixed.side = "BUY";
    fixed.expiration = "1";
    if (PreparedMarketOrderJson(fixed).valid()) return 9;
    return 0;
}
'''


def _openssl_flags() -> list[str]:
    pkg = shutil.which("pkg-config")
    if pkg:
        result = subprocess.run(
            [pkg, "--cflags", "--libs", "openssl"],
            check=False, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.split()
    return ["-lcrypto"]


def test_prepared_order_json_matches_generic_serializer() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        main = path / "main.cpp"
        binary = path / "prepared-order-test"
        main.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}",
            str(ROOT / "src/v7_clob_wire.cpp"),
            str(ROOT / "src/v7_clob_prepared_order.cpp"),
            str(main), "-o", str(binary), *_openssl_flags(),
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary)], check=True, timeout=10)
