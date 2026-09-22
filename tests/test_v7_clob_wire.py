from __future__ import annotations

import base64
import hashlib
import hmac
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_wire.hpp"
#include <array>
#include <iostream>
#include <string>
using namespace pm::v7::clob_wire;
int main() {
    SignedMarketOrderView order{
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
    PostMarketOrderView request{order, "owner-test-key", MarketOrderType::FAK};
    std::array<char, 2048> body{};
    const auto body_size = serialize_post_market_order(request, body);
    if (body_size == 0) return 2;
    std::cout.write(body.data(), static_cast<std::streamsize>(body_size));
    std::cout << "\n";

    L2HmacSigner signer("YWJj");
    if (!signer.valid()) return 3;
    std::array<char, 64> sig{};
    const auto sig_size = signer.sign("1789670000", std::string_view(body.data(), body_size), sig);
    if (sig_size == 0) return 4;
    std::cout.write(sig.data(), static_cast<std::streamsize>(sig_size));
    std::cout << "\n";

    // The production signer is intentionally reusable and single-owner.
    for (int i = 0; i < 4; ++i) {
        std::array<char, 64> repeated{};
        const auto repeated_size = signer.sign(
            "1789670000", std::string_view(body.data(), body_size), repeated);
        if (repeated_size != sig_size
            || std::string_view(repeated.data(), repeated_size)
                != std::string_view(sig.data(), sig_size)) return 8;
    }

    // HMAC keys longer than one SHA-256 block must first be hashed per RFC 2104.
    L2HmacSigner long_key(
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
        "MDEyMzQ1Njc4OTo7PD0+P0BBQkNERUZHSElKS0xNTk8=");
    if (!long_key.valid()) return 9;
    std::array<char, 64> long_sig{};
    const auto long_sig_size = long_key.sign(
        "1789670000", std::string_view(body.data(), body_size), long_sig);
    if (long_sig_size == 0) return 10;
    std::cout.write(long_sig.data(), static_cast<std::streamsize>(long_sig_size));
    std::cout << "\n";

    std::array<char, 16> too_small{};
    if (serialize_post_market_order(request, too_small) != 0) return 5;
    request.order.expiration = "1";
    if (serialize_post_market_order(request, body) != 0) return 6;
    request.order.expiration = "0";
    request.order.side = "BUY\"broken";
    if (serialize_post_market_order(request, body) != 0) return 7;
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


def test_native_clob_exact_body_and_l2_hmac() -> None:
    compiler = shutil.which("c++")
    assert compiler, "C++ compiler required by V7 repository"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main = tmp_path / "main.cpp"
        binary = tmp_path / "clob-wire-test"
        main.write_text(PROGRAM)
        command = [
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            f"-I{ROOT / 'include'}",
            str(ROOT / "src/v7_clob_wire.cpp"),
            str(main),
            "-o",
            str(binary),
            *_openssl_flags(),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)

    lines = result.stdout.splitlines()
    assert len(lines) == 3
    body, signature, long_signature = lines
    expected_body = (
        '{"deferExec":false,"postOnly":false,"order":{'
        '"builder":"0x0000000000000000000000000000000000000000000000000000000000000000",'
        '"expiration":"0",'
        '"maker":"0x1111111111111111111111111111111111111111",'
        '"makerAmount":"10000000",'
        '"metadata":"0x0000000000000000000000000000000000000000000000000000000000000000",'
        '"salt":479249096354,'
        '"side":"BUY",'
        '"signature":"0xabababab",'
        '"signatureType":3,'
        '"signer":"0x2222222222222222222222222222222222222222",'
        '"takerAmount":"19230800",'
        '"timestamp":"1789670000123",'
        '"tokenId":"71321045679252212594626385532706912750332728571942532289631379312455583992563"'
        '},"orderType":"FAK","owner":"owner-test-key"}'
    )
    assert body == expected_body
    message = ("1789670000" + "POST" + "/order" + body).encode()
    expected_signature = base64.urlsafe_b64encode(
        hmac.new(b"abc", message, hashlib.sha256).digest()
    ).decode()
    assert signature == expected_signature

    long_secret = bytes(range(80))
    expected_long_signature = base64.urlsafe_b64encode(
        hmac.new(long_secret, message, hashlib.sha256).digest()
    ).decode()
    assert long_signature == expected_long_signature

if __name__ == "__main__":
    test_native_clob_exact_body_and_l2_hmac()
