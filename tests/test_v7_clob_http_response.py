from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''#include "pm/v7_clob_http_response.hpp"
#include <cassert>
#include <iostream>
#include <string_view>
using namespace pm::v7::clob_http_response;
int main(){
 constexpr std::string_view fixed="HTTP/1.1 201 Created\r\nContent-Length: 17\r\nContent-Type: application/json\r\n\r\n{\"orderID\":\"abc\"}";
 for(std::size_t split=1; split<fixed.size(); ++split){
  ResponseParser p;
  assert(p.consume(std::span<const char>(fixed.data(),split),100));
  assert(p.consume(std::span<const char>(fixed.data()+split,fixed.size()-split),140));
  auto s=p.snapshot(); assert(s.complete==1 && s.http_success==1);
  assert(s.first_byte_monotonic_ns==100 && s.complete_monotonic_ns==140);
  assert(p.body()=="{\"orderID\":\"abc\"}");
 }
 constexpr std::string_view chunked="HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n4\r\n{\"ok\r\n4;foo=bar\r\n\":1}\r\n0\r\nX-Trace: a\r\n\r\n";
 for(std::size_t split=1; split<chunked.size(); ++split){
  ResponseParser p;
  assert(p.consume(std::span<const char>(chunked.data(),split),200));
  assert(p.consume(std::span<const char>(chunked.data()+split,chunked.size()-split),230));
  auto s=p.snapshot(); assert(s.complete==1 && s.http_success==1 && s.framing==Framing::Chunked);
  assert(p.body()=="{\"ok\":1}");
 }
 {
  ResponseParser p; constexpr std::string_view r="HTTP/1.1 204 No Content\r\nDate: x\r\n\r\n";
  assert(p.consume({r.data(),r.size()},300)); assert(p.snapshot().complete==1);
 }
 {
  ResponseParser p; constexpr std::string_view r="HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n";
  assert(!p.consume({r.data(),r.size()},400)); assert(p.snapshot().error==ParseError::ConflictingFraming);
 }
 {
  ResponseParser p; constexpr std::string_view r="HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc";
  assert(p.consume({r.data(),r.size()},500)); constexpr std::string_view x="x";
  assert(!p.consume({x.data(),x.size()},501)); assert(p.snapshot().error==ParseError::DataAfterComplete);
 }
 std::cout << "PASS\n";
}
'''


def test_bounded_incremental_response_parser() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        main = path / "main.cpp"
        binary = path / "response-test"
        main.write_text(PROGRAM)
        subprocess.run(
            [
                compiler,
                "-std=c++20",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                f"-I{ROOT / 'include'}",
                str(ROOT / "src/v7_clob_http_response.cpp"),
                str(main),
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            [str(binary)], check=True, capture_output=True, text=True
        )
    assert result.stdout == "PASS\n"
