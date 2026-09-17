from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_http1_response.hpp"
#include <algorithm>
#include <array>
#include <cstring>
#include <string>
#include <string_view>
using namespace pm::v7::clob_transport;
static bool feed(FixedHttp1Response& parser, std::string_view source, std::size_t step) {
    for (std::size_t pos = 0; pos < source.size();) {
        auto out = parser.writable(); if (out.empty()) return false;
        const auto count = std::min({step, source.size() - pos, out.size()});
        std::memcpy(out.data(), source.data() + pos, count); pos += count;
        const auto state = parser.commit(count);
        if (state != Http1ResponseState::Receiving && pos < source.size()) return false;
    }
    return true;
}
int main() {
    const std::string body = R"({"ok":true,"tradeIDs":["x"]})";
    const std::string good = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + std::to_string(body.size()) + "\r\nConnection: keep-alive\r\n\r\n" + body;
    for (const auto step : std::array<std::size_t, 6>{1, 2, 3, 7, 64, 4096}) {
        FixedHttp1Response parser;
        if (!feed(parser, good, step) || !parser.complete() || parser.status_code() != 200
            || parser.body() != body || parser.connection_close()) return 10;
    }
    { FixedHttp1Response p; const std::string s = "HTTP/1.1 403 Forbidden\r\ncontent-length: 3\r\nCONTENT-LENGTH: 3\r\nConnection: keep-alive, close\r\n\r\nbad";
      if (!feed(p, s, 5) || !p.complete() || p.body() != "bad" || !p.connection_close()) return 11; }
    { FixedHttp1Response p; const std::string s = "HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 4\r\n\r\nabc";
      (void)feed(p, s, 1000); if (p.state() != Http1ResponseState::Invalid) return 12; }
    { FixedHttp1Response p; const std::string s = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n";
      (void)feed(p, s, 1000); if (p.state() != Http1ResponseState::UnsupportedFraming) return 13; }
    { FixedHttp1Response p; const std::string s = "HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabcX";
      (void)feed(p, s, 1000); if (p.state() != Http1ResponseState::Invalid) return 14; }
    { FixedHttp1Response p; const std::string s = "HTTP/1.1 204 No Content\r\nConnection: keep-alive\r\n\r\n";
      if (!feed(p, s, 2) || !p.complete() || !p.body().empty()) return 15; }
    { FixedHttp1Response p; const std::string s = "HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n";
      (void)feed(p, s, 1000); if (p.state() != Http1ResponseState::UnsupportedFraming) return 16; }
    { FixedHttp1Response p; auto out = p.writable(); std::memset(out.data(), 'x', FixedHttp1Response::kMaxHeaderBytes + 1);
      if (p.commit(FixedHttp1Response::kMaxHeaderBytes + 1) != Http1ResponseState::Overflow) return 17; }
    return 0;
}'''


def test_bounded_http1_response_framing() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = tmp / "main.cpp"
        binary = tmp / "http1-response-test"
        main.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", str(ROOT / "src/v7_clob_http1_response.cpp"),
            str(main), "-o", str(binary),
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    test_bounded_http1_response_framing()
