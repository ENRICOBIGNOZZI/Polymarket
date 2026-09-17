#include "pm/v7_external_ws.hpp"
#include <boost/json.hpp>
#include <cassert>
#include <iostream>

using namespace pm::v7::external_fair;
namespace json = boost::json;

int main() {
    auto spec = coinbase_level2_connection_spec(500);
    assert(spec.host == "ws-feed.exchange.coinbase.com");
    assert(spec.port == "443");
    assert(spec.target == "/");
    assert(spec.symbol == "BTC-USD");
    assert(!spec.subscription_json.empty());
    auto request = json::parse(spec.subscription_json).as_object();
    assert(request.at("type").as_string() == "subscribe");
    assert(request.at("product_ids").as_array()[0].as_string() == "BTC-USD");
    assert(request.at("channels").as_array()[0].as_string() == "level2");
    assert(request.if_contains("key") == nullptr);
    assert(request.if_contains("signature") == nullptr);
    assert(request.if_contains("passphrase") == nullptr);
    auto advanced = coinbase_advanced_level2_connection_spec(500);
    assert(advanced.host == "advanced-trade-ws.coinbase.com");
    assert(advanced.symbol == "BTC-USD");
    assert(advanced.max_message_bytes >= 4U * 1024U * 1024U);
    auto advanced_request = json::parse(advanced.subscription_json).as_object();
    assert(advanced_request.at("channel").as_string() == "level2");
    assert(advanced_request.if_contains("jwt") == nullptr);
    std::cout << "Coinbase public level2 subscription PASS\n";
}
