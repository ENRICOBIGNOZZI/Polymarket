#include "pm/v7_external_ws.hpp"
#include <boost/json.hpp>
#include <cassert>
#include <iostream>

using namespace pm::v7::external_fair;
namespace json=boost::json;

int main(){
    auto spec=coinbase_level2_connection_spec(500);
    assert(spec.host=="ws-feed.exchange.coinbase.com");
    assert(spec.symbol=="BTC-USD");
    assert(spec.subscription_json.empty());
    assert(spec.subscription_auth==ExternalWsSubscriptionAuth::CoinbaseExchangeLevel2);
    const auto text=coinbase_level2_subscription_for_test(
        "BTC-USD","test-key","dGVzdC1zZWNyZXQtYnl0ZXM=","test-pass","1789650000.123456");
    auto value=json::parse(text).as_object();
    assert(value.at("type").as_string()=="subscribe");
    assert(value.at("key").as_string()=="test-key");
    assert(value.at("passphrase").as_string()=="test-pass");
    assert(value.at("timestamp").as_string()=="1789650000.123456");
    assert(value.at("signature").as_string()=="L639/NWT1iMA6gEdrRmzSVAFDMcR5X4K1Jf6Tt1DNdk=");
    const auto& channels=value.at("channels").as_array(); assert(channels.size()==1);
    const auto& channel=channels[0].as_object(); assert(channel.at("name").as_string()=="level2");
    assert(channel.at("product_ids").as_array()[0].as_string()=="BTC-USD");
    std::cout<<"Coinbase Exchange Level2 auth payload PASS\n";
}
