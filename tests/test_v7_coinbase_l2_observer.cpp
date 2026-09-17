#include "pm/v7_coinbase_l2_observer.hpp"
#include "pm/v7_ingress_wakeup.hpp"

#include <array>
#include <cassert>

using namespace pm::v7::external_fair;

int main() {
    IngressWakeup wakeup;
    ExternalVenueIngress ingress(VenueId::CoinbaseSpot, 500, nullptr, &wakeup);
    CoinbaseL2FrameObserver observer(ingress, 500);
    observer.on_connection_epoch(7);

    constexpr auto snapshot = R"({"type":"snapshot","product_id":"BTC-USD","bids":[["100.00","2.0"],["99.00","4.0"]],"asks":[["101.00","3.0"],["102.00","5.0"]]})";
    observer.on_frame(7, 1'000, 2'000, snapshot);

    std::array<ExternalVenueEvent, 16> events{};
    auto count = ingress.drain_events(events);
    assert(count == 2);
    assert(events[0].event_type == ExternalEventType::Health);
    assert(events[0].healthy == 0);
    assert(events[1].event_type == ExternalEventType::BookTop);
    assert(events[1].healthy == 1);
    assert(events[1].bid == 100.0 && events[1].ask == 101.0);
    assert(events[1].bid_size == 2.0 && events[1].ask_size == 3.0);

    constexpr auto update = R"({"type":"l2update","product_id":"BTC-USD","changes":[["buy","100.00","0"],["buy","99.00","6.0"],["sell","101.00","1.5"]]})";
    observer.on_frame(7, 1'100, 2'100, update);
    count = ingress.drain_events(events);
    assert(count == 1);
    assert(events[0].bid == 99.0 && events[0].ask == 101.0);
    assert(events[0].bid_size == 6.0 && events[0].ask_size == 1.5);
    assert(observer.metrics().valid == 1);

    constexpr auto malformed = R"({"type":"l2update","changes":[["bad","99","1"]]})";
    observer.on_frame(7, 1'200, 2'200, malformed);
    const auto failed = observer.metrics();
    assert(failed.valid == 0);
    assert(failed.parse_failures == 1);
    return 0;
}
