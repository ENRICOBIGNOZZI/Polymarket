#include "pm/v7_maker_lane.hpp"

#include <cassert>
#include <chrono>

namespace {

std::int64_t monotonic_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

pm::v7::maker::MakerModelSnapshot profitable_model() {
    pm::v7::maker::MakerModelSnapshot model;
    model.tick_size = 0.01;
    model.fill_coefficients.fill(0.0);
    model.fill_coefficients[0] = 3.0;
    model.markout_coefficients.fill(0.0);
    model.base_ev_se_per_share = 0.0;
    model.robust_ev_z = 0.0;
    model.inventory_cost_per_share = 0.0;
    model.latency_cost_per_second = 0.0;
    model.base_quote_shares = 1.0;
    model.min_quote_lifetime_ns = 0;
    model.toxicity_withdraw_threshold = 0.99;
    model.one_sided_inventory_fraction = 0.50;
    return model;
}

pm::v7::MarketWsEvent book_event(std::int32_t tick_e4 = 100) {
    pm::v7::MarketWsEvent event;
    event.kind = pm::v7::MarketWsEventKind::BookChanged;
    event.market_handle = 11;
    event.event_handle = 22;
    event.instrument_handle = 33;
    event.state_version = 7;
    event.exchange_event_ns = 1'700'000'000'000'000'000LL;
    event.receive_monotonic_ns = monotonic_ns();
    event.book.state_version = 7;
    event.book.exchange_event_ns = event.exchange_event_ns;
    event.book.receive_monotonic_ns = event.receive_monotonic_ns;
    event.book.tick_size_e4 = tick_e4;
    event.book.best_bid_e4 = 4800;
    event.book.best_ask_e4 = 5200;
    event.book.best_bid_microunits = 20'000'000;
    event.book.best_ask_microunits = 20'000'000;
    event.book.bid_depth = {20'000'000, 100'000'000, 150'000'000};
    event.book.ask_depth = {20'000'000, 100'000'000, 150'000'000};
    event.book.lineage_continuous = 1;
    event.book.valid = 1;
    return event;
}

pm::v7::maker::MakerLaneContext default_context() {
    pm::v7::maker::MakerLaneContext context;
    context.risk.max_quote_shares = 1.0;
    context.risk.max_abs_residual_shares = 10.0;
    return context;
}

pm::v7::maker::MakerLaneContext context_with_positive_yes_inventory() {
    auto context = default_context();
    context.inventory.yes_shares = 8.0;
    context.inventory.no_shares = 0.0;
    return context;
}

void refresh_timestamp(pm::v7::MarketWsEvent& event) {
    event.receive_monotonic_ns = monotonic_ns();
    event.book.receive_monotonic_ns = event.receive_monotonic_ns;
    ++event.state_version;
    event.book.state_version = event.state_version;
}

void test_yes_lane_positive_yes_inventory_quotes_reducing_sell_side() {
    pm::v7::maker::MakerInstrumentLane lane(+1);
    const auto decision = lane.on_market_event(
        book_event(), context_with_positive_yes_inventory(), profitable_model());
    assert(decision.action == pm::v7::maker::Action::OneSided);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Quote);
    assert(decision.intents[0].side == pm::v7::Side::Sell);
}

void test_no_lane_positive_yes_inventory_quotes_reducing_buy_side() {
    pm::v7::maker::MakerInstrumentLane lane(-1);
    const auto decision = lane.on_market_event(
        book_event(), context_with_positive_yes_inventory(), profitable_model());
    assert(decision.action == pm::v7::maker::Action::OneSided);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Quote);
    // Buying NO reduces YES-NO exposure, so the NO lane must admit BUY and
    // suppress SELL when the global residual is strongly positive.
    assert(decision.intents[0].side == pm::v7::Side::Buy);
}

void test_lane_uses_venue_tick_size_not_stale_model_tick() {
    pm::v7::maker::MakerInstrumentLane lane(+1);
    auto model = profitable_model();
    model.tick_size = 0.01; // deliberately stale relative to event tick.
    auto event = book_event(10); // venue tick 0.001 => 480 / 520 tick indexes.
    const auto decision = lane.on_market_event(event, default_context(), model);
    assert(decision.reason == pm::v7::maker::DecisionReason::Quote);
    assert(decision.intent_count == 2);
    for (std::size_t i = 0; i < decision.intent_count; ++i) {
        const auto& intent = decision.intents[i];
        if (intent.side == pm::v7::Side::Buy) assert(intent.price_tick < 520);
        if (intent.side == pm::v7::Side::Sell) assert(intent.price_tick > 480);
    }
}

void test_trade_event_enters_incremental_trade_intensity() {
    pm::v7::maker::MakerInstrumentLane lane(+1);
    auto event = book_event();
    (void)lane.on_market_event(event, default_context(), profitable_model());
    event.kind = pm::v7::MarketWsEventKind::Trade;
    event.side = pm::v7::Side::Buy;
    event.quantity_microunits = 5'000'000;
    refresh_timestamp(event);
    const auto decision = lane.on_market_event(event, default_context(), profitable_model());
    assert(decision.features.trade_intensity > 0.0);
}

void test_book_depth_removal_enters_cancel_pressure() {
    pm::v7::maker::MakerInstrumentLane lane(+1);
    auto event = book_event();
    (void)lane.on_market_event(event, default_context(), profitable_model());

    event.book.bid_depth.l5_microunits = 70'000'000;
    event.book.ask_depth.l5_microunits = 90'000'000;
    refresh_timestamp(event);
    const auto decision = lane.on_market_event(event, default_context(), profitable_model());
    assert(decision.features.cancel_intensity > 0.0);
}

void test_public_print_is_not_double_counted_as_cancel_pressure() {
    pm::v7::maker::MakerInstrumentLane lane(+1);
    auto event = book_event();
    (void)lane.on_market_event(event, default_context(), profitable_model());

    event.kind = pm::v7::MarketWsEventKind::Trade;
    event.side = pm::v7::Side::Buy; // consumes asks
    event.quantity_microunits = 10'000'000;
    refresh_timestamp(event);
    const auto trade = lane.on_market_event(event, default_context(), profitable_model());
    assert(trade.features.trade_intensity > 0.0);
    assert(trade.features.cancel_intensity == 0.0);

    event.kind = pm::v7::MarketWsEventKind::BookChanged;
    event.side = pm::v7::Side::None;
    event.quantity_microunits = 0;
    event.book.ask_depth.l5_microunits = 90'000'000;
    refresh_timestamp(event);
    const auto book = lane.on_market_event(event, default_context(), profitable_model());
    assert(book.features.cancel_intensity == 0.0);
}

void test_invalid_lineage_forces_feed_withdraw() {
    pm::v7::maker::MakerInstrumentLane lane(+1);
    auto event = book_event();
    event.kind = pm::v7::MarketWsEventKind::LineageInvalidated;
    event.book.valid = 0;
    event.book.lineage_continuous = 0;
    const auto decision = lane.on_market_event(event, default_context(), profitable_model());
    assert(decision.reason == pm::v7::maker::DecisionReason::FeedUnhealthy);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Withdraw);
}

void test_invalid_inventory_orientation_fails_closed() {
    pm::v7::maker::MakerInstrumentLane lane(0);
    const auto decision = lane.on_market_event(book_event(), default_context(), profitable_model());
    assert(decision.reason == pm::v7::maker::DecisionReason::FeedUnhealthy);
}

void test_rejected_quote_preserves_best_robust_ev_for_diagnostics() {
    pm::v7::maker::MakerInstrumentLane lane(+1);
    auto model = profitable_model();
    model.fill_coefficients[0] = -4.0;
    model.base_ev_se_per_share = 0.01;
    model.robust_ev_z = 2.0;
    const auto decision = lane.on_market_event(book_event(), default_context(), model);
    assert(decision.reason == pm::v7::maker::DecisionReason::NoEconomicQuote);
    assert(decision.robust_ev < 0.0);
    assert(decision.ev_uncertainty > 0.0);
}

} // namespace

int main() {
    test_yes_lane_positive_yes_inventory_quotes_reducing_sell_side();
    test_no_lane_positive_yes_inventory_quotes_reducing_buy_side();
    test_lane_uses_venue_tick_size_not_stale_model_tick();
    test_trade_event_enters_incremental_trade_intensity();
    test_book_depth_removal_enters_cancel_pressure();
    test_public_print_is_not_double_counted_as_cancel_pressure();
    test_invalid_lineage_forces_feed_withdraw();
    test_invalid_inventory_orientation_fails_closed();
    test_rejected_quote_preserves_best_robust_ev_for_diagnostics();
    return 0;
}
