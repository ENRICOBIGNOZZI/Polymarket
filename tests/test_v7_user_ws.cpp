#include "pm/v7_user_ws.hpp"

#include <array>
#include <cassert>
#include <string_view>

using namespace pm::v7;

int main() {
    UserWsParser parser;
    std::array<UserWsEvent, 8> output{};
    const pm::fast::FeedReceiveStamp stamp{1'782'753'357'257LL, 9'000'000'000LL};

    constexpr std::string_view order = R"({
      "event_type":"order",
      "id":"0xorder",
      "market":"0xmarket",
      "asset_id":"123",
      "side":"BUY",
      "original_size":"10",
      "size_matched":"0",
      "price":"0.52",
      "type":"PLACEMENT",
      "status":"LIVE",
      "timestamp":"1782753357257"
    })";
    auto parsed = parser.parse(order, stamp, output);
    assert(parsed.invalid_frame == 0 && parsed.output_count == 1);
    assert(output[0].kind == UserWsEventKind::Order);
    assert(output[0].order_action == UserOrderAction::Placement);
    assert(output[0].order_status == UserOrderStatus::Live);
    assert(output[0].side == Side::Buy);
    assert(output[0].price_e4 == 5'200);
    assert(output[0].original_size_microunits == 10'000'000);
    assert(output[0].order_id.view() == "0xorder");
    assert(output[0].receive_monotonic_ns == stamp.monotonic_ns);

    constexpr std::string_view taker_trade = R"({
      "event_type":"trade",
      "id":"trade-1",
      "taker_order_id":"0xorder",
      "market":"0xmarket",
      "asset_id":"123",
      "side":"BUY",
      "size":"2.5",
      "price":"0.52",
      "status":"MATCHED",
      "timestamp":"1782753357258"
    })";
    parsed = parser.parse(taker_trade, stamp, output);
    assert(parsed.invalid_frame == 0 && parsed.output_count == 1);
    assert(output[0].kind == UserWsEventKind::Trade);
    assert(output[0].trade_status == UserTradeStatus::Matched);
    assert(output[0].trade_size_microunits == 2'500'000);
    assert(output[0].order_id.view() == "0xorder");
    assert(output[0].taker_order_id.view() == "0xorder");

    constexpr std::string_view maker_trade = R"({
      "event_type":"trade",
      "id":"trade-2",
      "taker_order_id":"foreign-taker",
      "market":"0xmarket",
      "asset_id":"123",
      "side":"SELL",
      "size":"5",
      "price":"0.51",
      "status":"MATCHED",
      "timestamp":"1782753357259",
      "maker_orders":[
        {"order_id":"our-maker","matched_amount":"1.25"},
        {"order_id":"other-maker","matched_amount":"3.75"}
      ]
    })";
    parsed = parser.parse(maker_trade, stamp, output);
    assert(parsed.invalid_frame == 0 && parsed.output_count == 3);
    assert(output[0].order_id.view() == "foreign-taker");
    assert(output[1].order_id.view() == "our-maker");
    assert(output[1].trade_size_microunits == 1'250'000);
    assert(output[2].order_id.view() == "other-maker");
    assert(output[2].trade_size_microunits == 3'750'000);

    constexpr std::string_view wrapped = R"({
      "type":"order",
      "payload":{
        "id":"wrapped-order",
        "market":"0xmarket",
        "asset_id":"123",
        "side":"SELL",
        "original_size":"3",
        "size_matched":"1",
        "price":"0.49",
        "type":"UPDATE",
        "status":"LIVE",
        "timestamp":1782753357260
      }
    })";
    parsed = parser.parse(wrapped, stamp, output);
    assert(parsed.invalid_frame == 0 && parsed.output_count == 1);
    assert(output[0].order_action == UserOrderAction::Update);
    assert(output[0].side == Side::Sell);
    assert(output[0].size_matched_microunits == 1'000'000);

    parsed = parser.parse("not-json", stamp, output);
    assert(parsed.invalid_frame != 0);
    return 0;
}
