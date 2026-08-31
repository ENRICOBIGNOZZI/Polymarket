#include "pm/v7_external_state.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

using namespace pm::v7::external_fair;

namespace {

OracleEvent oracle_event(std::uint64_t seq, std::uint64_t epoch, std::int64_t receive_ns,
                         bool reconnect = false, bool gap = false,
                         bool recovery = false) {
    OracleEvent event;
    event.feed_handle = 77;
    event.source_sequence = seq;
    event.connection_epoch = epoch;
    event.exact_decimal_handle = 1000 + seq;
    event.value_numeric = 100.0 + 0.01 * static_cast<double>(seq);
    event.oracle_observation_ns = receive_ns - 5;
    event.publisher_timestamp_ns = receive_ns - 4;
    event.local_receive_monotonic_ns = receive_ns;
    event.local_receive_wall_ns = receive_ns + 1'000;
    event.window_seconds = 60;
    event.healthy = 1;
    event.reconnect = reconnect ? 1 : 0;
    event.gap = gap ? 1 : 0;
    event.same_oracle_recovery = recovery ? 1 : 0;
    return event;
}

ExternalVenueEvent book(VenueId venue, std::uint64_t seq, std::int64_t receive_ns,
                        double bid, double ask, double bid_size, double ask_size) {
    ExternalVenueEvent event;
    event.asset_handle = 500;
    event.source_sequence = seq;
    event.connection_epoch = 1;
    event.venue = venue;
    event.event_type = ExternalEventType::BookTop;
    event.exchange_event_ns = receive_ns - 10;
    event.local_receive_monotonic_ns = receive_ns;
    event.local_receive_wall_ns = receive_ns + 1'000;
    event.bid = bid;
    event.ask = ask;
    event.bid_size = bid_size;
    event.ask_size = ask_size;
    event.healthy = 1;
    return event;
}

ExternalVenueEvent derivative_context(VenueId venue, std::int64_t receive_ns) {
    ExternalVenueEvent event;
    event.asset_handle = 500;
    event.connection_epoch = 1;
    event.venue = venue;
    event.event_type = ExternalEventType::DerivativeContext;
    event.local_receive_monotonic_ns = receive_ns;
    event.local_receive_wall_ns = receive_ns + 1'000;
    event.mark_price = 100.1;
    event.index_price = 100.0;
    event.funding_rate = -0.0001;
    event.open_interest = 123.0;
    event.context_valid_mask = DerivativeContextMarkPrice | DerivativeContextIndexPrice
        | DerivativeContextFundingRate | DerivativeContextOpenInterest;
    event.healthy = 1;
    return event;
}

} // namespace

int main() {
    OracleState oracle;
    assert(oracle.on_event(oracle_event(1, 1, 100)));
    auto first = oracle.snapshot(105, 1000);
    assert(first.valid == 0);
    assert(first.continuity == OracleContinuity::ContinuityUnknown);

    assert(oracle.on_event(oracle_event(2, 1, 110)));
    auto continuous = oracle.snapshot(115, 1000);
    assert(continuous.valid == 1);
    assert(continuous.continuity == OracleContinuity::LiveContinuous);

    assert(oracle.on_event(oracle_event(1, 2, 120, true)));
    auto disconnected = oracle.snapshot(125, 1000);
    assert(disconnected.valid == 0);
    assert(disconnected.continuity == OracleContinuity::ContinuityUnknown);

    assert(oracle.on_event(oracle_event(2, 2, 130, false, false, true)));
    auto recovered = oracle.snapshot(135, 1000);
    assert(recovered.valid == 1);
    assert(recovered.continuity == OracleContinuity::RecoveredSameOracleSnapshot);

    // Stale/duplicate source sequence in the same epoch is rejected.
    assert(!oracle.on_event(oracle_event(2, 2, 131)));

    ExternalStatePolicy policy;
    policy.min_healthy_venues = 2;
    policy.max_venue_age_ns = 1'000;
    ExternalAssetState external(500);
    assert(external.on_venue_event(book(VenueId::BinanceSpot, 1, 200, 99.9, 100.1, 10, 8), policy));
    assert(external.on_venue_event(book(VenueId::CoinbaseSpot, 1, 205, 100.0, 100.2, 7, 9), policy));
    auto two = external.snapshot(210, policy);
    assert(two.valid == 1);
    assert(two.venue_count_fresh == 2);
    assert(two.venue_composite_price > 99.9 && two.venue_composite_price < 100.2);

    assert(external.on_venue_event(book(VenueId::BybitSpot, 1, 215, 100.1, 100.3, 5, 6), policy));
    auto three = external.snapshot(220, policy);
    assert(three.valid == 1);
    assert(three.venue_count_fresh == 3);
    assert(three.venue_health_mask == 0x7);
    assert(three.venue_dispersion_bps >= 0.0);
    assert(std::isfinite(three.aggregate_ofi));

    // One dislocated but syntactically valid feed must not pull a weighted
    // mean away from the independent venue cluster. Its raw event is still
    // accepted and taped; only composite membership is excluded.
    ExternalAssetState robust(500);
    assert(robust.on_venue_event(book(VenueId::BinanceSpot, 1, 200, 99.9, 100.1, 1, 1), policy));
    assert(robust.on_venue_event(book(VenueId::CoinbaseSpot, 1, 201, 100.0, 100.2, 1, 1), policy));
    assert(robust.on_venue_event(book(VenueId::BybitSpot, 1, 202, 129.9, 130.1, 1, 1), policy));
    const auto robust_snapshot = robust.snapshot(203, policy);
    assert(robust_snapshot.valid == 1);
    assert(robust_snapshot.venue_count_fresh == 2);
    assert(robust_snapshot.venue_health_mask == 0x3);
    assert(robust_snapshot.venue_outlier_mask == 0x4);
    assert(robust_snapshot.venue_weighted_median_price > 99.9 && robust_snapshot.venue_weighted_median_price < 100.2);
    assert(robust_snapshot.venue_max_residual_bps > 2'000.0);
    assert(robust_snapshot.venue_composite_price > 99.9 && robust_snapshot.venue_composite_price < 100.2);
    auto outlier_trade = book(VenueId::BybitSpot, 1, 203, 0.0, 0.0, 0.0, 0.0);
    outlier_trade.event_type = ExternalEventType::Trade;
    outlier_trade.trade_price = 130.0;
    outlier_trade.trade_size = 10'000.0;
    outlier_trade.trade_side = 1;
    assert(robust.on_venue_event(outlier_trade, policy));
    assert(std::abs(robust.snapshot(203, policy).aggregate_trade_imbalance) < 1e-12);

    // Binance bookTicker and aggregate-trade IDs are different source
    // sequence domains. A trade must not invalidate a subsequent book update
    // or be rejected merely because its ID is below the latest book ID.
    auto independent_trade = book(VenueId::BinanceSpot, 1, 216, 0.0, 0.0, 0.0, 0.0);
    independent_trade.event_type = ExternalEventType::Trade;
    independent_trade.trade_price = 100.0;
    independent_trade.trade_size = 1.0;
    independent_trade.trade_side = 1;
    assert(external.on_venue_event(independent_trade, policy));
    assert(!external.on_venue_event(book(VenueId::BinanceSpot, 1, 217, 99.9, 100.1, 10, 8), policy));
    assert(external.on_venue_event(book(VenueId::BinanceSpot, 2, 217, 99.9, 100.1, 10, 8), policy));

    assert(external.on_venue_event(book(VenueId::Deribit, 1, 218, 100.05, 100.25, 4, 4), policy));
    auto four = external.snapshot(220, policy);
    assert(four.valid == 1);
    assert(four.venue_count_fresh == 3);
    assert(four.venue_health_mask == 0x7);

    assert(external.on_venue_event(book(VenueId::BybitLinear, 1, 219, 100.0, 100.2, 3, 3), policy));
    const auto contextual_derivative = external.snapshot(220, policy);
    assert(contextual_derivative.venue_count_fresh == 3);
    assert(contextual_derivative.venue_health_mask == 0x7);

    // Derivative observations carry their own freshness and stay zero-weight
    // contextual inputs; they neither replace nor refresh an executable book.
    assert(external.on_venue_event(derivative_context(VenueId::Deribit, 220), policy));
    assert(external.on_venue_event(derivative_context(VenueId::BybitLinear, 221), policy));
    const auto with_context = external.snapshot(222, policy);
    assert(with_context.derivative_contexts[0].venue == VenueId::Deribit);
    assert(with_context.derivative_contexts[0].valid_mask
        == (DerivativeContextMarkPrice | DerivativeContextIndexPrice
            | DerivativeContextFundingRate | DerivativeContextOpenInterest));
    assert(std::abs(with_context.derivative_contexts[0].funding_rate + 0.0001) < 1e-12);
    assert(with_context.derivative_contexts[1].venue == VenueId::BybitLinear);
    assert(with_context.derivative_contexts[1].open_interest == 123.0);
    assert(with_context.venue_composite_price > 99.9 && with_context.venue_composite_price < 100.2);

    ExternalStatePolicy short_age = policy;
    short_age.max_venue_age_ns = 10;
    ExternalAssetState book_freshness(500);
    assert(book_freshness.on_venue_event(book(VenueId::BinanceSpot, 1, 300, 99.9, 100.1, 1, 1), short_age));
    assert(book_freshness.on_venue_event(book(VenueId::CoinbaseSpot, 1, 301, 100.0, 100.2, 1, 1), short_age));
    auto late_trade = book(VenueId::BinanceSpot, 1, 320, 0.0, 0.0, 0.0, 0.0);
    late_trade.event_type = ExternalEventType::Trade;
    late_trade.trade_price = 100.0;
    late_trade.trade_size = 1.0;
    late_trade.trade_side = 1;
    assert(book_freshness.on_venue_event(late_trade, short_age));
    assert(book_freshness.snapshot(320, short_age).valid == 0);

    external.on_oracle_snapshot(recovered);
    auto with_oracle = external.snapshot(221, policy);
    assert(with_oracle.chainlink_feed_handle == recovered.feed_handle);
    assert(with_oracle.chainlink_continuity == OracleContinuity::RecoveredSameOracleSnapshot);

    // A gap makes one price-contributing venue unavailable. Deribit remains
    // contextual at its default zero composite weight, so a three-source
    // requirement fails closed rather than silently using two sources.
    policy.min_healthy_venues = 3;
    auto bad = book(VenueId::BinanceSpot, 3, 230, 99.9, 100.1, 10, 8);
    bad.gap = 1;
    assert(external.on_venue_event(bad, policy));
    auto insufficient = external.snapshot(231, policy);
    assert(insufficient.valid == 0);
    assert(insufficient.venue_count_fresh == 2);

    CausalStateInputs inputs;
    inputs.pm_state_version = 1;
    inputs.oracle_state_version = 2;
    inputs.external_state_version = 3;
    inputs.settlement_reference_version = 4;
    inputs.inventory_state_version = 5;
    inputs.private_state_version = 6;
    inputs.risk_state_version = 7;
    inputs.pm_receive_ns = 10;
    inputs.oracle_receive_ns = 20;
    inputs.external_receive_ns = 30;
    inputs.settlement_reference_receive_ns = 15;
    inputs.inventory_receive_ns = 25;
    inputs.private_receive_ns = 26;
    inputs.risk_receive_ns = 27;
    auto cut = make_causal_cut(1, 31, TriggerSource::ExternalPriceUpdate, 3, inputs);
    assert(cut.valid == 1);
    assert(cut.max_input_receive_monotonic_ns == 30);

    inputs.external_receive_ns = 32; // future relative to decision
    auto future = make_causal_cut(2, 31, TriggerSource::ExternalPriceUpdate, 4, inputs);
    assert(future.valid == 0);

    std::cout << "v7 external state tests passed\n";
    return 0;
}
