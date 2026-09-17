from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_user_ws.hpp"
#include <array>
#include <cassert>
#include <iostream>
using namespace pm::v7::user_ws;
int main(){
 auto o=decode(R"({"event_type":"order","id":"0xORDER","market":"0xCOND","asset_id":"TOKEN","side":"SELL","price":"0.57","original_size":"10","size_matched":"0","type":"PLACEMENT"})",100);
 assert(o.recognized && !o.invalid && o.event.kind==EventKind::Order);
 assert(o.event.order_type==OrderEventType::Placement && !o.event.buy_side);
 auto t=decode(R"({"event_type":"trade","id":"trade-uuid","market":"0xCOND","asset_id":"TOKEN","side":"BUY","size":"10","price":"0.57","status":"MATCHED","type":"TRADE"})",120);
 assert(t.recognized && !t.invalid && t.event.kind==EventKind::Trade);
 assert(t.event.trade_status==TradeStatus::Matched && t.event.buy_side);
 auto p=decode("PONG",130);
 assert(p.recognized && !p.invalid && p.event.kind==EventKind::Pong);
 std::array<std::string_view,1> markets{"0xCOND"};
 auto s=subscription_json({"key-test","secret-test","pass-test"},markets);
 assert(s.find("\"apiKey\":\"key-test\"")!=std::string::npos);
 assert(s.find("\"markets\":[\"0xCOND\"]")!=std::string::npos);
 assert(s.find("\"type\":\"user\"")!=std::string::npos);
 assert(decode("not-json",140).invalid);
 assert(kHeartbeatIntervalMs==10000 && kHeartbeatRequest=="PING");
 std::cout << "PASS\n";
 return 0;
}
'''


def test_user_ws_codec_and_subscription() -> None:
    cxx = shutil.which("c++")
    assert cxx
    boost = subprocess.check_output(["brew", "--prefix", "boost"], text=True).strip()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        source = path / "main.cpp"
        binary = path / "user-ws-test"
        source.write_text(PROGRAM)
        subprocess.run(
            [
                cxx,
                "-std=c++20",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                f"-I{ROOT / 'include'}",
                f"-I{boost}/include",
                str(ROOT / "src/v7_user_ws.cpp"),
                str(ROOT / "src/boost_json.cpp"),
                str(source),
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
