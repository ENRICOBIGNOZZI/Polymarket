#include "pm/v7_coinbase_l2_observer.hpp"
#include "pm/v7_ingress_wakeup.hpp"

#include <array>
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <new>
#include <thread>

namespace { std::atomic<std::uint64_t> allocations{0}; }
void* operator new(std::size_t size) {
    allocations.fetch_add(1, std::memory_order_relaxed);
    if (void* value=std::malloc(size)) return value;
    throw std::bad_alloc();
}
void operator delete(void* value) noexcept { std::free(value); }
void operator delete(void* value, std::size_t) noexcept { std::free(value); }

using namespace pm::v7::external_fair;

int main() {
    IngressWakeup wakeup;
    ExternalVenueIngress ingress(VenueId::CoinbaseSpot, 500, nullptr, &wakeup);
    CoinbaseL2FrameObserver observer(ingress, 500);
    observer.on_connection_epoch(7);

    constexpr auto snapshot = R"({"type":"snapshot","product_id":"BTC-USD","bids":[["100.00","2.0"],["99.00","4.0"]],"asks":[["101.00","3.0"],["102.00","5.0"]]})";
    const auto allocation_before_snapshot = allocations.load(std::memory_order_relaxed);
    observer.on_frame(7, 1'000, 2'000, snapshot);
    const auto allocation_after_snapshot = allocations.load(std::memory_order_relaxed);
    assert(allocation_after_snapshot == allocation_before_snapshot);

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
    const auto allocation_before_update = allocations.load(std::memory_order_relaxed);
    observer.on_frame(7, 1'100, 2'100, update);
    const auto allocation_after_update = allocations.load(std::memory_order_relaxed);
    assert(allocation_after_update == allocation_before_update);
    count = ingress.drain_events(events);
    assert(count == 1);
    assert(events[0].bid == 99.0 && events[0].ask == 101.0);
    assert(events[0].bid_size == 6.0 && events[0].ask_size == 1.5);
    assert(observer.metrics().valid == 1);

    // Cold telemetry may read concurrently, but it must never contend with or
    // observe a torn hot-book snapshot.
    std::atomic<bool> reader_failed{false};
    std::thread reader([&] {
        for (int i = 0; i < 20'000; ++i) {
            const auto m = observer.metrics();
            if (m.valid != 0 && (!(m.best_bid > 0.0) || !(m.best_ask > m.best_bid))) {
                reader_failed.store(true, std::memory_order_relaxed);
                break;
            }
        }
    });
    for (int i = 0; i < 2'000; ++i) {
        observer.on_frame(7, 2'000 + i, 3'000 + i, (i & 1) ? update : snapshot);
        (void)ingress.drain_events(events);
    }
    reader.join();
    assert(!reader_failed.load(std::memory_order_relaxed));

    constexpr auto malformed = R"({"type":"l2update","changes":[["bad","99","1"]]})";
    observer.on_frame(7, 1'200, 2'200, malformed);
    const auto failed = observer.metrics();
    assert(failed.valid == 0);
    assert(failed.parse_failures == 1);
    return 0;
}
