from __future__ import annotations

import shutil
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = (
    "0xa3a093c83b6c20c83355c16ce94c92e6e9fcbdeb840618cc74f6c57a42ad145b"
    "2b98db73d2c73cbf1f2b6af288566ae81960ddbc3a13921027358a8bff3be6ff1c"
    "a440cbd865bc0c6243d7a8df9a8bf48a8827b0a4abbb61c30e96d305423af148"
    "d23d42d3ad94e65d78258cecaf8dcbaddac0f73dc085040f2c12bb595dd83804"
    "4f726465722875696e743235362073616c742c61646472657373206d616b65722c"
    "61646472657373207369676e65722c75696e7432353620746f6b656e49642c75"
    "696e74323536206d616b6572416d6f756e742c75696e743235362074616b6572"
    "416d6f756e742c75696e743820736964652c75696e7438207369676e61747572"
    "65547970652c75696e743235362074696d657374616d702c6279746573333220"
    "6d657461646174612c62797465733332206275696c6465722900ba"
)
PROGRAM = r'''
#include "pm/v7_poly1271.hpp"
#include <array>
#include <cstdio>
#include <cstring>

using namespace pm::v7;

int main() {
    constexpr std::array<std::uint8_t, 32> key = {
        0xac,0x09,0x74,0xbe,0xc3,0x9a,0x17,0xe3,
        0x6b,0xa4,0xa6,0xb4,0xd2,0x38,0xff,0x94,
        0x4b,0xac,0xb4,0x78,0xcb,0xed,0x5e,0xfc,
        0xae,0x78,0x4d,0x7b,0xf4,0xf2,0xff,0x80};
    poly1271::Secp256k1Signer signer(key);
    if (!signer.valid()) return 2;
    std::array<char, 42> address{};
    if (!signer.address_hex(address)) return 3;
    constexpr char expected_address[] = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266";
    if (std::memcmp(address.data(), expected_address, 42) != 0) return 4;
    clob_eip712::ExchangeV2DomainView domain{
        80002, "0xE111180000d2663C0091e4f400237545B87B996B"};
    constexpr auto wallet = "0x1111111111111111111111111111111111111111";
    poly1271::Poly1271OrderHasher hasher(domain, wallet);
    if (!hasher.valid()) return 5;
    clob_eip712::ExchangeV2OrderView order{
        "479249096354", wallet, wallet, "1234", "100000000", "50000000",
        0, 3, "1710000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000"};
    std::array<char, poly1271::kWrappedSignatureHexChars> output{};
    if (!poly1271::sign_poly1271_hex(hasher, signer, order, output)) return 6;
    std::fwrite(output.data(), 1, output.size(), stdout);
    std::fputc('\n', stdout);
    return 0;
}
'''


def _crypto_flags() -> tuple[list[str], list[str]]:
    secp_cflags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "libsecp256k1"], text=True))
    secp_libs = shlex.split(subprocess.check_output(
        ["pkg-config", "--libs", "libsecp256k1"], text=True))
    brew = shutil.which("brew")
    if brew:
        prefix = subprocess.check_output([brew, "--prefix", "openssl@3"], text=True).strip()
        return [f"-I{prefix}/include", *secp_cflags], [*secp_libs, f"-L{prefix}/lib", "-lcrypto"]
    return secp_cflags, [*secp_libs, "-lcrypto"]
def test_poly1271_matches_official_rust_sdk_vector() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        main = tmp / "main.cpp"
        binary = tmp / "poly1271-test"
        main.write_text(PROGRAM)
        inc, libs = _crypto_flags()
        command = [
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", *inc,
            str(ROOT / "src/v7_keccak_fast.cpp"),
            str(ROOT / "src/v7_clob_eip712.cpp"),
            str(ROOT / "src/v7_poly1271.cpp"),
            str(main), "-o", str(binary), *libs,
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        result = subprocess.run([str(binary)], check=True, capture_output=True,
                                text=True, timeout=10)
        assert result.stdout.strip() == EXPECTED
        assert len(EXPECTED) == 636
