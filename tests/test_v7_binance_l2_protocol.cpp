#include "pm/v7_binance_l2_protocol.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

using namespace pm::v7::external_fair;

int main() {
    BinanceDepthDelta delta;
    const auto parsed = parse_binance_spot_depth_delta(
        R"({"e":"depthUpdate","E":1672515782136,"s":"BTCUSDT","U":101,"u":103,"b":[["65000.1","1.2"],["64999.9","0"]],"a":[["65000.2","2.3"]]})",
        100, delta);
    assert(parsed == BinanceDepthParseState::Parsed);
    assert(delta.first_update_id == 101 && delta.final_update_id == 103);
    assert(delta.source_event_ns == 1672515782136000000LL);
    assert(delta.bids.size() == 2 && std::abs(delta.asks[0].quantity - 2.3) < 1e-12);

    BinanceDepthSnapshot snapshot;
    assert(parse_binance_depth_snapshot(
        R"({"lastUpdateId":100,"bids":[["65000.1","1.2"]],"asks":[["65000.2","2.3"]]})", 101, snapshot)
        == BinanceDepthParseState::Parsed);
    assert(snapshot.last_update_id == 100 && snapshot.bids.size() == 1);

    assert(parse_binance_spot_depth_delta(R"({"result":null,"id":1})", 102, delta)
           == BinanceDepthParseState::Ignored);
    assert(parse_binance_spot_depth_delta(R"({"e":"depthUpdate","U":5,"u":4,"b":[],"a":[]})", 102, delta)
           == BinanceDepthParseState::Invalid);
    assert(parse_binance_depth_snapshot(R"({"lastUpdateId":10,"bids":[["1","1"],["2","1"]],"asks":[]})", 102, snapshot, 1)
           == BinanceDepthParseState::TooLarge);
    std::cout << "v7 Binance L2 protocol tests passed\n";
}
