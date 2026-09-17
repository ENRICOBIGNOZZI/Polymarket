from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_post_builder.hpp"
#include <array>
#include <iostream>
#include <string_view>
using namespace pm::v7;
int main() {
    clob_wire::SignedMarketOrderView order{
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0",
        "0x1111111111111111111111111111111111111111",
        "10000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "479249096354",
        "BUY",
        "0xabababab",
        3,
        "0x2222222222222222222222222222222222222222",
        "19230800",
        "1789670000123",
        "71321045679252212594626385532706912750332728571942532289631379312455583992563"
    };
    clob_wire::PostMarketOrderView request{order, "owner-test-key", clob_wire::MarketOrderType::FAK};
    constexpr std::string_view address = "0x3333333333333333333333333333333333333333";
    constexpr std::string_view api_key = "api-key-test";
    constexpr std::string_view passphrase = "pass-test";
    constexpr std::string_view timestamp = "1789670000";
    constexpr std::string_view secret = "YWJj";

    std::array<char, 2048> body{};
    const auto body_size = clob_wire::serialize_post_market_order(request, body);
    if (body_size == 0) return 2;
    clob_wire::L2HmacSigner signer(secret);
    if (!signer.valid()) return 3;
    std::array<char, clob_wire::L2HmacSigner::kEncodedSignatureSize> sig{};
    const auto sig_size = signer.sign(timestamp, std::string_view(body.data(), body_size), sig);
    if (sig_size != sig.size()) return 4;

    clob_http_frame::L2AuthHeadersView auth{
        address, std::string_view(sig.data(), sig_size), timestamp, api_key, passphrase};
    std::array<char, 4096> reference{};
    const auto reference_size = clob_http_frame::serialize_post_order_http1(
        auth, std::string_view(body.data(), body_size), reference);
    if (reference_size == 0) return 5;

    clob_post::PreparedPostOrderBuilder builder(address, api_key, passphrase, secret);
    if (!builder.valid()) return 6;
    std::array<char, 4096> fused{};
    const auto fused_size = builder.build(request, timestamp, fused);
    if (fused_size != reference_size) return 7;
    if (std::string_view(fused.data(), fused_size)
        != std::string_view(reference.data(), reference_size)) return 8;

    std::array<char, 64> small{};
    if (builder.build(request, timestamp, small) != 0) return 9;
    request.order.side = "BUY\"broken";
    if (builder.build(request, timestamp, fused) != 0) return 10;
    request.order.side = "BUY";
    if (builder.build(request, "1x", fused) != 0) return 11;

    std::cout.write(fused.data(), static_cast<std::streamsize>(fused_size));
    return 0;
}
'''


def _openssl_flags() -> list[str]:
    pkg = shutil.which("pkg-config")
    if pkg:
        result = subprocess.run(
            [pkg, "--cflags", "--libs", "openssl"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.split()
    return ["-lcrypto"]


def test_zero_copy_post_builder_matches_composed_reference() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        main = path / "main.cpp"
        binary = path / "post-builder-test"
        main.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}",
            str(ROOT / "src/v7_clob_wire.cpp"),
            str(ROOT / "src/v7_clob_http_frame.cpp"),
            str(ROOT / "src/v7_clob_post_builder.cpp"),
            str(main), "-o", str(binary), *_openssl_flags(),
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary)], check=True, capture_output=True)
