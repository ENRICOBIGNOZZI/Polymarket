from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_http_frame.hpp"
#include <array>
#include <iostream>
#include <string_view>
using namespace pm::v7::clob_http_frame;
int main(){
 L2AuthHeadersView a{"0x1111111111111111111111111111111111111111","YWJjZA==","1789670000","api-key-test","pass-test"};
 constexpr std::string_view body="{\"orderType\":\"FAK\"}";
 std::array<char,1024> out{};
 auto n=serialize_post_order_http1(a,body,out); if(!n)return 2;
 PreparedPostOrderHttp1 prepared(a.address,a.api_key,a.passphrase); if(!prepared.valid())return 6;
 std::array<char,1024> fast{};
 auto fast_n=prepared.serialize(a.signature,a.timestamp,body,fast); if(fast_n!=n)return 7;
 if(std::string_view(fast.data(),fast_n)!=std::string_view(out.data(),n))return 8;
 std::cout.write(out.data(),static_cast<std::streamsize>(n));
 std::array<char,8> small{}; if(serialize_post_order_http1(a,body,small)!=0)return 3;
 if(prepared.serialize(a.signature,a.timestamp,body,small)!=0)return 12;
 a.api_key="bad\r\nInjected: 1"; if(serialize_post_order_http1(a,body,out)!=0)return 4;
 a.api_key="api-key-test"; a.timestamp="1x"; if(serialize_post_order_http1(a,body,out)!=0)return 5;
 if(prepared.serialize("bad\r\nInjected: 1","1789670000",body,fast)!=0)return 9;
 if(prepared.serialize("YWJjZA==","1x",body,fast)!=0)return 10;
 PreparedPostOrderHttp1 invalid(a.address,"bad\r\nInjected: 1",a.passphrase); if(invalid.valid())return 11;
 return 0;
}
'''


def test_bounded_http1_order_frame() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main = tmp_path / "main.cpp"
        binary = tmp_path / "frame-test"
        main.write_text(PROGRAM)
        subprocess.run(
            [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
             f"-I{ROOT / 'include'}", str(ROOT / "src/v7_clob_http_frame.cpp"),
             str(main), "-o", str(binary)],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run([str(binary)], check=True, capture_output=True)

    body = '{"orderType":"FAK"}'
    expected = (
        "POST /order HTTP/1.1\r\n"
        "Host: clob.polymarket.com\r\n"
        "Content-Type: application/json\r\n"
        "Accept: application/json\r\n"
        "POLY_ADDRESS: 0x1111111111111111111111111111111111111111\r\n"
        "POLY_SIGNATURE: YWJjZA==\r\n"
        "POLY_TIMESTAMP: 1789670000\r\n"
        "POLY_API_KEY: api-key-test\r\n"
        "POLY_PASSPHRASE: pass-test\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: keep-alive\r\n\r\n"
        + body
    )
    assert result.stdout == expected.encode()
