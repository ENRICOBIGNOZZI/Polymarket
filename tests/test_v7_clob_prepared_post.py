from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_prepared_post.hpp"
#include <array>
#include <string_view>
using namespace pm::v7;

bool run_case(std::string_view side, clob_wire::MarketOrderType type,
              std::string_view maker_amount, std::string_view salt,
              std::string_view order_signature, std::string_view taker_amount,
              std::string_view order_timestamp) {
    clob_wire::SignedMarketOrderView order{
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0",
        "0x1111111111111111111111111111111111111111",
        maker_amount,
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        salt,
        side,
        order_signature,
        3,
        "0x2222222222222222222222222222222222222222",
        taker_amount,
        order_timestamp,
        "71321045679252212594626385532706912750332728571942532289631379312455583992563"
    };
    clob_wire::PostMarketOrderView request{order, "owner-test-key", type};
    clob_wire::PreparedMarketOrderStaticView fixed{
        order.builder, order.expiration, order.maker, order.metadata, order.side,
        order.signature_type, order.signer, order.token_id, request.owner, request.order_type};
    clob_wire::MarketOrderDynamicView dynamic{
        order.maker_amount, order.salt_decimal, order.signature,
        order.taker_amount, order.timestamp_ms};

    constexpr std::string_view address = "0x3333333333333333333333333333333333333333";
    constexpr std::string_view api_key = "api-key-test";
    constexpr std::string_view passphrase = "pass-test";
    constexpr std::string_view request_timestamp = "1789670000";
    constexpr std::string_view secret = "YWJj";

    // Reference composed path: generic JSON -> HMAC -> generic HTTP frame.
    std::array<char, 2048> body{};
    const auto body_size = clob_wire::serialize_post_market_order(request, body);
    if (body_size == 0) return false;
    clob_wire::L2HmacSigner signer(secret);
    if (!signer.valid()) return false;
    std::array<char, clob_wire::L2HmacSigner::kEncodedSignatureSize> l2_signature{};
    const auto l2_size = signer.sign(
        request_timestamp, std::string_view(body.data(), body_size), l2_signature);
    if (l2_size != l2_signature.size()) return false;
    clob_http_frame::L2AuthHeadersView auth{
        address, std::string_view(l2_signature.data(), l2_size), request_timestamp,
        api_key, passphrase};
    std::array<char, 4096> reference{};
    const auto reference_size = clob_http_frame::serialize_post_order_http1(
        auth, std::string_view(body.data(), body_size), reference);
    if (reference_size == 0) return false;

    // Fused path: prepared static JSON + in-place body + HMAC + backfilled HTTP.
    clob_post::PreparedPostOrderBuilder builder(
        fixed, address, api_key, passphrase, secret);
    if (!builder.valid()) return false;
    std::array<char, 4096> fused{};
    const auto fused_size = builder.build(dynamic, request_timestamp, fused);
    return fused_size == reference_size
        && std::string_view(fused.data(), fused_size)
            == std::string_view(reference.data(), reference_size);
}

int main() {
    if (!run_case("BUY", clob_wire::MarketOrderType::FAK,
                  "10000000", "479249096354", "0xabababab",
                  "19230800", "1789670000123")) return 2;
    if (!run_case("SELL", clob_wire::MarketOrderType::FOK,
                  "19230800", "18446744073709551615", "0xcdcdcdcdcdcd",
                  "10000000", "1789670000999")) return 3;

    clob_wire::PreparedMarketOrderStaticView fixed{
        "0x00", "0", "0x1111111111111111111111111111111111111111", "0x00",
        "BUY", 3, "0x2222222222222222222222222222222222222222",
        "1234", "owner", clob_wire::MarketOrderType::FAK};
    clob_post::PreparedPostOrderBuilder builder(
        fixed, "0x3333333333333333333333333333333333333333",
        "api", "pass", "YWJj");
    if (!builder.valid()) return 4;
    std::array<char, 2048> out{};
    if (builder.build({"1x", "2", "0xab", "3", "4"}, "1789670000", out) != 0) return 5;
    if (builder.build({"1", "2", "bad\"sig", "3", "4"}, "1789670000", out) != 0) return 6;
    if (builder.build({"1", "2", "0xab", "3", "4"}, "1x", out) != 0) return 7;
    std::array<char, 32> small{};
    if (builder.build({"1", "2", "0xab", "3", "4"}, "1789670000", small) != 0) return 8;
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


def test_prepared_zero_copy_post_matches_composed_reference() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        main = path / "main.cpp"
        binary = path / "prepared-post-test"
        main.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}",
            str(ROOT / "src/v7_clob_wire.cpp"),
            str(ROOT / "src/v7_clob_prepared_order.cpp"),
            str(ROOT / "src/v7_clob_http_frame.cpp"),
            str(ROOT / "src/v7_clob_prepared_post.cpp"),
            str(main), "-o", str(binary), *_openssl_flags(),
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary)], check=True, timeout=10)
