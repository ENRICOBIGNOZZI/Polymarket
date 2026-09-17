from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_eip712.hpp"
#include <array>
#include <iostream>
#include <string_view>
using namespace pm::v7::clob_eip712;
void show(const Hash32& h) { std::array<char,64> out{}; if(!hash32_hex(h,out)) std::exit(9); std::cout.write(out.data(),64); std::cout << "\n"; }
int main(){
    show(keccak256(std::string_view{}));
    ExchangeV2DomainView domain{80002, "0xE111180000d2663C0091e4f400237545B87B996B"};
    ExchangeV2OrderView order{
        "479249096354",
        "0x1111111111111111111111111111111111111111",
        "0x1111111111111111111111111111111111111111",
        "1234", "100000000", "50000000", 0, 3,
        "1710000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000000000000000000000000000"
    };
    Hash32 a{}, b{}, c{};
    if(!exchange_v2_domain_separator(domain,a)) return 2;
    if(!exchange_v2_order_struct_hash(order,b)) return 3;
    ExchangeV2OrderHasher hasher(domain);
    if(!hasher.valid() || hasher.domain_separator() != a) return 4;
    if(!hasher.digest(order,c)) return 8;
    show(a); show(b); show(c);
    order.token_id_decimal = "-1";
    if(exchange_v2_order_struct_hash(order,b)) return 5;
    order.token_id_decimal = "1234";
    order.maker = "0x1234";
    if(exchange_v2_order_struct_hash(order,b)) return 6;
    order.maker = "0x1111111111111111111111111111111111111111";
    order.signature_type = 4;
    if(exchange_v2_order_struct_hash(order,b)) return 7;
    return 0;
}
'''


def test_native_exchange_v2_eip712_matches_official_sdk_fixture() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main = tmp_path / "main.cpp"
        binary = tmp_path / "eip712-test"
        main.write_text(PROGRAM)
        subprocess.run(
            [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
             f"-I{ROOT / 'include'}", str(ROOT / "src/v7_clob_eip712.cpp"), str(main),
             "-o", str(binary)],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)

    assert result.stdout.splitlines() == [
        # Canonical Ethereum Keccak-256 of the empty byte string.
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        # Official py-clob-client-v2 POLY_1271 fixture embeds these exact
        # app-domain and Order contents hashes in its expected signature.
        "a440cbd865bc0c6243d7a8df9a8bf48a8827b0a4abbb61c30e96d305423af148",
        "d23d42d3ad94e65d78258cecaf8dcbaddac0f73dc085040f2c12bb595dd83804",
        # Independently computed with eth-account encode_typed_data from the
        # same official fixture before freezing this regression vector.
        "2afca94626db91b2d556c351584a4cd93e13da32e377dc48c7983e43afa5ab47",
    ]
