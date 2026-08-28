#include "pm/v7_external_ingress.hpp"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <string>

using namespace pm::v7::external_fair;

namespace {

std::string book(std::uint64_t sequence, double bid, double ask) {
    return "{\"u\":" + std::to_string(sequence)
        + ",\"s\":\"BTCUSDT\",\"b\":\"" + std::to_string(bid)
        + "\",\"B\":\"1.0\",\"a\":\"" + std::to_string(ask)
        + "\",\"A\":\"1.0\"}";
}

} // namespace

int main() {
    ExternalStatePolicy policy;
    policy.min_healthy_venues = 1;
    policy.max_venue_age_ns = 10'000'000'000LL;

    ExternalAssetState state(500);
    ExternalVenueIngress ingress(VenueId::BinanceSpot, 500);

    auto first = ingress.on_frame(1, 100, 1'000, book(1, 65000.0, 65001.0));
    assert(first.invalid_frame == 0);
    assert(first.output_count == 1);
    assert(ingress.drain_into(state, policy) == 1);
    auto state_snapshot = state.snapshot(101, policy);
    assert(state_snapshot.valid == 1);
    assert(state_snapshot.venue_count_fresh == 1);

    auto ingress_snapshot = ingress.snapshot();
    assert(ingress_snapshot.frames == 1);
    assert(ingress_snapshot.enqueued_events == 1);
    assert(ingress_snapshot.drained_events == 1);
    assert(ingress_snapshot.dropped_events == 0);
    assert(ingress_snapshot.connection_epoch == 1);

    // A new connection epoch is explicitly a continuity break. The first event
    // in the new epoch carries reconnect+gap into the single-writer state.
    auto reconnected = ingress.on_frame(2, 200, 1'100, book(1, 65002.0, 65003.0));
    assert(reconnected.invalid_frame == 0);
    assert(reconnected.output_count == 1);
    assert(ingress.drain_into(state, policy) == 1);
    ingress_snapshot = ingress.snapshot();
    assert(ingress_snapshot.reconnects == 1);
    assert(ingress_snapshot.propagated_gaps == 1);

    // The state does not silently treat a post-gap numerical value as clean
    // continuity. A later clean same-epoch packet is required before the venue
    // can again contribute as a fresh source.
    auto after_gap = state.snapshot(201, policy);
    assert(after_gap.valid == 0);
    assert(after_gap.venue_count_fresh == 0);

    auto clean = ingress.on_frame(2, 210, 1'110, book(2, 65002.5, 65003.5));
    assert(clean.invalid_frame == 0);
    assert(ingress.drain_into(state, policy) == 1);
    auto recovered = state.snapshot(211, policy);
    assert(recovered.valid == 1);
    assert(recovered.venue_count_fresh == 1);

    // Backpressure is bounded and non-blocking. Saturate the queue without a
    // consumer: some events must be dropped and the next accepted event must
    // carry a propagated gap rather than hiding the loss.
    ExternalAssetState overloaded_state(500);
    ExternalVenueIngress overloaded(VenueId::BinanceSpot, 500);
    for (std::uint64_t i = 1; i <= kExternalIngressQueueCapacity + 64; ++i) {
        (void)overloaded.on_frame(1, 1'000 + static_cast<std::int64_t>(i),
                                  2'000 + static_cast<std::int64_t>(i),
                                  book(i, 65000.0, 65001.0));
    }
    auto overloaded_snapshot = overloaded.snapshot();
    assert(overloaded_snapshot.dropped_events > 0);
    assert(overloaded_snapshot.gap_pending == 1);
    assert(overloaded_snapshot.queued <= kExternalIngressQueueCapacity);

    const auto drained = overloaded.drain_into(overloaded_state, policy);
    assert(drained <= kExternalIngressQueueCapacity);
    auto recovery_frame = overloaded.on_frame(
        1, 10'000, 20'000, book(kExternalIngressQueueCapacity + 100, 65010.0, 65011.0));
    assert(recovery_frame.output_count == 1);
    (void)overloaded.drain_into(overloaded_state, policy);
    overloaded_snapshot = overloaded.snapshot();
    assert(overloaded_snapshot.propagated_gaps >= 1);
    assert(overloaded_snapshot.gap_pending == 0);

    overloaded.mark_disconnected(2);
    overloaded_snapshot = overloaded.snapshot();
    assert(overloaded_snapshot.healthy == 0);
    assert(overloaded_snapshot.gap_pending == 1);

    std::cout << "v7 external ingress tests passed\n";
    return 0;
}
