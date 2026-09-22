#include "pm/v7_user_oms_bridge.hpp"

#include <algorithm>
#include <array>
#include <cassert>
#include <filesystem>
#include <string_view>

using namespace pm::v7;

namespace {
template <std::size_t N>
void set_text(user_ws::FixedText<N>& out, std::string_view value) {
    assert(value.size() <= out.data.size());
    std::copy(value.begin(), value.end(), out.data.begin());
    out.size = static_cast<std::uint16_t>(value.size());
}
}

int main() {
    const auto path = std::filesystem::temp_directory_path()
        / "pm-v7-user-ws-latency-test.bin";
    std::error_code ec;
    std::filesystem::remove(path, ec);
    NativeLatencyTape tape(path.string());
    UserOmsBridge bridge(&tape);
    std::array<RoutedOmsEvent, 8> routed{};

    const auto ack = bridge.on_post_order_ack(
        42,
        R"({"success":true,"orderID":"ex-42","status":"live"})",
        100,
        routed);
    assert(!ack.invalid_ack);
    assert(ack.output_count == 1);
    assert(routed[0].client_order_id == 42);
    assert(routed[0].event.type == OmsEventType::AckLive);
    assert(routed[0].event.timestamp_ns == 100);
    assert(routed[0].event.user_ws_match_monotonic_ns == 0);

    user_ws::Event matched{};
    matched.kind = user_ws::EventKind::Trade;
    matched.trade_status = user_ws::TradeStatus::Matched;
    matched.receive_monotonic_ns = 150;
    set_text(matched.id, "trade-1");
    set_text(matched.taker_order_id, "ex-42");
    set_text(matched.trade_size, "1.25");

    routed = {};
    const auto user = bridge.on_user_event(matched, routed);
    assert(user.output_count == 1);
    assert(routed[0].client_order_id == 42);
    assert(routed[0].event.type == OmsEventType::FillDelta);
    assert(routed[0].event.timestamp_ns == 150);
    assert(routed[0].event.user_ws_match_monotonic_ns == 150);
    assert(routed[0].event.fill_delta_microunits == 1'250'000);
    tape.stop();
    assert(std::filesystem::file_size(path) == sizeof(NativeLatencyEvent));
    std::filesystem::remove(path, ec);
    return 0;
}
