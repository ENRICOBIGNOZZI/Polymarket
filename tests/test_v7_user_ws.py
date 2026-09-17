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
 auto o=decode(R"({"event_type":"order","id":"0xORDER","market":"0xCOND","asset_id":"TOKEN","side":"SELL","price":"0.57","original_size":"10","size_matched":"0","type":"PLACEMENT","status":"LIVE"})",100);
 assert(o.recognized && !o.invalid && o.event.kind==EventKind::Order);
 assert(o.event.order_type==OrderEventType::Placement && !o.event.buy_side);

 auto t=decode(R"({"event_type":"trade","id":"trade-uuid","taker_order_id":"taker-123","market":"0xCOND","asset_id":"TOKEN","side":"BUY","size":"10","price":"0.57","status":"MATCHED","type":"TRADE","trader_side":"taker","maker_orders":[{"order_id":"maker-a","owner":"owner","matched_amount":"2.5","price":"0.57","asset_id":"TOKEN","side":"SELL"},{"order_id":"maker-b","owner":"owner","matched_amount":"7.5","price":"0.57","asset_id":"TOKEN","side":"SELL"}]})",120);
 assert(t.recognized && !t.invalid && t.event.kind==EventKind::Trade);
 assert(t.event.trade_status==TradeStatus::Matched && t.event.buy_side);
 assert(t.event.trader_is_taker && !t.event.trader_is_maker);
 assert(t.event.taker_order_id.view()=="taker-123");
 assert(t.event.maker_order_count==2);
 assert(t.event.maker_orders[0].order_id.view()=="maker-a");
 assert(t.event.maker_orders[0].matched_amount.view()=="2.5");
 assert(!t.event.maker_orders[0].buy_side);
 assert(t.event.maker_orders[1].order_id.view()=="maker-b");

 auto envelope=decode(R"({"topic":"user","type":"trade","payload":{"id":"trade-env","taker_order_id":"taker-env","market":"0xCOND","asset_id":"TOKEN","side":"SELL","size":"1","price":"0.43","status":"CONFIRMED","trader_side":"MAKER","maker_orders":[]}})",121);
 assert(envelope.recognized && !envelope.invalid);
 assert(envelope.event.trade_status==TradeStatus::Confirmed);
 assert(envelope.event.trader_is_maker && !envelope.event.trader_is_taker);
 assert(envelope.event.taker_order_id.view()=="taker-env");

 auto p=decode("PONG",130);
 assert(p.recognized && !p.invalid && p.event.kind==EventKind::Pong);
 std::array<std::string_view,1> markets{"0xCOND"};
 auto s=subscription_json({"key-test","secret-test","pass-test"},markets);
 assert(s.find("\"apiKey\":\"key-test\"")!=std::string::npos);
 assert(s.find("\"markets\":[\"0xCOND\"]")!=std::string::npos);
 assert(s.find("\"type\":\"user\"")!=std::string::npos);
 assert(decode("not-json",140).invalid);
 assert(decode(R"({"event_type":"trade","id":"x","market":"m","asset_id":"a","side":"BUY","size":"1","price":"0.5","status":"MATCHED"})",141).invalid);
 assert(kHeartbeatIntervalMs==10000 && kHeartbeatRequest=="PING");
 std::cout << "PASS\n";
 return 0;
}
'''


def test_user_ws_codec_and_subscription() -> None:
    cxx = shutil.which("c++")
    assert cxx
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
