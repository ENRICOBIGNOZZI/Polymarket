#include "pm/v7_ingress_wakeup.hpp"
#include "pm/v7_external_ingress.hpp"

#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <iostream>
#include <memory>
#include <thread>
#include <vector>

using namespace pm::v7::external_fair;
using namespace std::chrono_literals;

int main() {
    IngressWakeup wakeup;
    assert(!wakeup.wait_for(0ms));
    // A signal published while the consumer is active is syscall-free and
    // must remain visible: no lost wakeup.
    const auto kernel_before = wakeup.kernel_wakeups();
    wakeup.notify();
    assert(wakeup.kernel_wakeups() == kernel_before);
    assert(wakeup.wait_for(0ms));
    assert(!wakeup.wait_for(0ms));
    assert(!wakeup.wait_for(-1ms));

    // Signal saturation is coalescing, never a blocking producer or an error.
    const auto burst_kernel_before = wakeup.kernel_wakeups();
    for (int i = 0; i < 100000; ++i) wakeup.notify();
    assert(wakeup.kernel_wakeups() == burst_kernel_before);
    assert(wakeup.wait_for(0ms));
    while (wakeup.wait_for(0ms)) {}
    assert(wakeup.errors() == 0);

    // Userspace spin catches a producer without requiring an fd wake.
    std::thread spin_producer([&] {
        std::this_thread::sleep_for(100us);
        wakeup.notify();
    });
    assert(wakeup.wait_for(5ms, 1000us));
    spin_producer.join();

    // Multiple independent venue producers may signal the same consumer.
    std::atomic<int> finished{0};
    std::vector<std::thread> producers;
    for (int p = 0; p < 4; ++p) {
        producers.emplace_back([&] {
            for (int i = 0; i < 10000; ++i) wakeup.notify();
            finished.fetch_add(1, std::memory_order_release);
            wakeup.notify();
        });
    }
    while (finished.load(std::memory_order_acquire) != 4) (void)wakeup.wait_for(5ms);
    for (auto& producer : producers) producer.join();
    while (wakeup.wait_for(0ms)) {}
    assert(wakeup.errors() == 0);

    // Notification is only a hint; authoritative events stay in the original
    // SPSC queue. Verify exact sequence and cardinality through the real ingress.
    auto ingress = std::make_unique<ExternalVenueIngress>(VenueId::BinanceSpot, 500, nullptr, &wakeup);
    std::atomic<bool> done{false};
    std::thread producer([&] {
        for (std::uint64_t i = 1; i <= 1000; ++i) {
            ExternalVenueEvent event;
            event.venue = VenueId::BinanceSpot;
            event.asset_handle = 500;
            event.connection_epoch = 1;
            event.source_sequence = i;
            event.local_receive_monotonic_ns = static_cast<std::int64_t>(i);
            event.local_receive_wall_ns = static_cast<std::int64_t>(i + 1000);
            event.event_type = ExternalEventType::BookTop;
            event.bid = 65000.0; event.ask = 65001.0;
            event.bid_size = 1; event.ask_size = 1; event.healthy = 1;
            const bool accepted = ingress->on_event(event);
            assert(accepted);
        }
        done.store(true, std::memory_order_release);
        wakeup.notify();
    });
    std::array<ExternalVenueEvent, 64> batch;
    std::uint64_t received = 0;
    while (!done.load(std::memory_order_acquire) || ingress->snapshot().queued != 0) {
        const auto count = ingress->drain_events(batch);
        for (std::size_t i = 0; i < count; ++i) {
            assert(batch[i].source_sequence == ++received);
            assert(batch[i].gap == 0);
        }
        if (count == 0) (void)wakeup.wait_for(5ms);
    }
    producer.join();
    assert(received == 1000);
    assert(ingress->snapshot().dropped_events == 0);
    assert(wakeup.errors() == 0);
    std::cout << "ingress wakeup: syscall-free hot notify, hybrid spin, concurrent producers and exact queue delivery PASS\n";
}
