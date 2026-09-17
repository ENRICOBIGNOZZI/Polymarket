from __future__ import annotations

import shutil
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
INNER = EXPECTED[2:132]
EXPECTED_INNER_DIGEST = "c31d535831b64decf4687570628e5765c218fc3c4a7b582c7bd3105525f76a8c"

PROGRAM = rf'''
#include "pm/v7_clob_eip712.hpp"
#include "pm/v7_clob_poly1271.hpp"
#include <array>
#include <iostream>
using namespace pm::v7::clob_eip712;
using namespace pm::v7::clob_poly1271;
int main() {{
    ExchangeV2DomainView domain{{80002, "0xE111180000d2663C0091e4f400237545B87B996B"}};
    ExchangeV2OrderView order{{
        "479249096354",
        "0x1111111111111111111111111111111111111111",
        "0x1111111111111111111111111111111111111111",
        "1234", "100000000", "50000000", 0, 3, "1710000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000"
    }};
    Hash32 app{{}}, contents{{}};
    if (!exchange_v2_domain_separator(domain, app)) return 2;
    if (!exchange_v2_order_struct_hash(order, contents)) return 3;
    PreparedInnerDigest prepared(domain, order.signer);
    if (!prepared.valid()) return 7;
    Hash32 inner_digest{{}};
    if (!prepared.digest(contents, inner_digest)) return 8;
    std::array<char, 64> digest_hex{{}};
    if (!hash32_hex(inner_digest, digest_hex)) return 9;
    std::cout.write(digest_hex.data(), static_cast<std::streamsize>(digest_hex.size()));
    std::cout << '\n';
    PreparedInnerDigest bad(domain, "0x1234");
    if (bad.valid()) return 10;
    std::array<char, 1024> output{{}};
    const auto size = wrap_signature("{INNER}", app, contents, output);
    if (size == 0) return 4;
    std::cout.write(output.data(), static_cast<std::streamsize>(size));
    std::cout << '\n';
    if (wrap_signature("1234", app, contents, output) != 0) return 5;
    std::array<char, 16> small{{}};
    if (wrap_signature("{INNER}", app, contents, small) != 0) return 6;
    return 0;
}}
'''


def test_poly1271_wrapper_matches_official_fixture() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "poly1271-test"
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}",
            str(ROOT / "src/v7_clob_eip712.cpp"),
            str(ROOT / "src/v7_keccak_fast.cpp"),
            str(ROOT / "src/v7_clob_poly1271.cpp"),
            "-x", "c++", "-", "-o", str(binary),
        ], input=PROGRAM, text=True, check=True, capture_output=True)
        result = subprocess.run(
            [str(binary)], check=True, capture_output=True, text=True,
        )
    assert result.stdout.splitlines() == [EXPECTED_INNER_DIGEST, EXPECTED]
    assert len(EXPECTED) == 2 + 130 + 64 + 64 + 2 * 186 + 4
