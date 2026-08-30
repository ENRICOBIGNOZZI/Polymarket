#include "pm/v7_maker_execution_policy.hpp"

#include <cassert>
#include <cmath>

namespace {

constexpr std::uint64_t kMarket = 11;
constexpr std::uint64_t kYes = 101;
constexpr std::uint64_t kNo = 102;
constexpr std::int32_t kTickE4 = 100;

pm::v7::CapitalLimits capital_limits(std::int64_t total = 5'000'000) {
    pm::v7::CapitalLimits out;
    out.sleeve_budget_microdollars = total;
    out.max_total_exposure_microdollars = total;
    out.max_market_exposure_microdollars = total;
    out.max_single_order_microdollars = total;
    return out;
}

pm::v7::ExecutionPlan quote_plan(std::uint64_t intent_id,
                                 std::uint64_t instrument,
                                 pm::v7::Side side,
                                 std::int64_t price_tick,
                                 std::int64_t quantity,
                                 std::int64_t decision_ns = 1'000'000'000LL,
                                 std::int64_t exchange_ns = 10'000'000'000LL) {
    pm::v7::ExecutionPlan plan;
    plan.intent.intent_id = intent_id;
    plan.intent.market_handle = kMarket;
    plan.intent.event_handle = 22;
    plan.intent.instrument_handle = instrument;
    plan.intent.strategy_id = pm::v7::StrategyId::ProfessionalMaker;
    plan.intent.type = pm::v7::IntentType::Quote;
    plan.intent.side = side;
    plan.intent.price_tick = price_tick;
    plan.intent.quantity_microunits = quantity;
    plan.intent.decision_monotonic_ns = decision_ns;
    plan.intent.exchange_event_ns = exchange_ns;
    plan.intent.passive = 1;
    plan.intent.post_only = 1;
    plan.intent.urgency = pm::v7::Urgency::Passive;
    plan.tick_size_e4 = kTickE4;
    plan.market_state_version = 1;
    plan.policy = pm::v7::ExecutionPolicyId::PassiveMaker;
    plan.public_queue_observable = 1;
    plan.public_queue_ahead_microunits = 0;
    return plan;
}

pm::v7::ExecutionPlan cancel_plan(std::uint64_t intent_id,
                                  std::uint64_t instrument,
                                  pm::v7::Side side,
                                  std::int64_t decision_ns) {
    auto plan = quote_plan(intent_id, instrument, side, 1, 1, decision_ns,
                           10'000'000'000LL + decision_ns);
    plan.intent.type = pm::v7::IntentType::CancelQuote;
    plan.intent.price_tick = 0;
    plan.intent.quantity_microunits = 0;
    plan.intent.urgency = pm::v7::Urgency::Critical;
    plan.policy = pm::v7::ExecutionPolicyId::Emergency;
    plan.public_queue_observable = 0;
    plan.tick_size_e4 = 0;
    return plan;
}

pm::v7::PublicTradePrint trade(std::uint64_t trade_id,
                               std::uint64_t instrument,
                               pm::v7::Side aggressor,
                               std::int64_t price_tick,
                               std::int64_t quantity,
                               std::int64_t exchange_ns,
                               std::int64_t receive_ns) {
    pm::v7::PublicTradePrint out;
    out.trade_id = trade_id;
    out.instrument_handle = instrument;
    out.aggressor_side = aggressor;
    out.price_tick = price_tick;
    out.quantity_microunits = quantity;
    out.exchange_event_ns = exchange_ns;
    out.receive_monotonic_ns = receive_ns;
    return out;
}

pm::v7::maker::MakerPaperExecutionPolicy policy() {
    pm::v7::maker::MakerPaperExecutionPolicy out;
    assert(out.register_market(kMarket, kYes, kNo, kTickE4));
    return out;
}

void test_full_buy_fill_moves_reservation_to_inventory() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    const auto submitted = execution.process(
        quote_plan(1, kYes, pm::v7::Side::Buy, 48, 1'000'000), capital);
    assert(submitted.accepted);
    assert(capital.snapshot().order_reserved_microdollars == 480'000);
    assert(capital.snapshot().inventory_committed_microdollars == 0);

    const auto filled = execution.on_public_trade(
        kMarket,
        trade(101, kYes, pm::v7::Side::Sell, 48, 1'000'000,
              10'100'000'000LL, 1'100'000'000LL),
        capital);
    assert(filled.accepted);
    const auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 0);
    assert(snap.inventory_committed_microdollars == 480'000);
    assert(snap.total_exposure_microdollars == 480'000);
    assert(execution.inventory(kMarket)->yes_microunits == 1'000'000);
}

void test_partial_fill_preserves_total_capital_until_cancel_effective() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    assert(execution.process(
        quote_plan(2, kYes, pm::v7::Side::Buy, 48, 2'000'000), capital).accepted);
    assert(capital.snapshot().total_exposure_microdollars == 960'000);

    assert(execution.on_public_trade(
        kMarket,
        trade(102, kYes, pm::v7::Side::Sell, 48, 500'000,
              10'100'000'000LL, 1'100'000'000LL),
        capital).accepted);
    auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 720'000);
    assert(snap.inventory_committed_microdollars == 240'000);
    assert(snap.total_exposure_microdollars == 960'000);

    assert(execution.process(
        cancel_plan(200, kYes, pm::v7::Side::Buy, 1'200'000'000LL), capital).accepted);
    // Cancel-requested is still fillable, therefore residual reservation remains.
    assert(capital.snapshot().order_reserved_microdollars == 720'000);

    assert(execution.advance_time(kMarket, 1'400'000'000LL, capital).accepted);
    snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 0);
    assert(snap.inventory_committed_microdollars == 240'000);
    assert(snap.total_exposure_microdollars == 240'000);
}

void test_control_plane_cancel_all_releases_buy_reservations() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    assert(execution.process(
        quote_plan(205, kYes, pm::v7::Side::Buy, 48, 2'000'000), capital).accepted);
    assert(capital.snapshot().order_reserved_microdollars == 960'000);
    assert(execution.cancel_all(kMarket, 1'200'000'000LL, capital).accepted);
    assert(capital.snapshot().order_reserved_microdollars == 960'000);
    assert(execution.advance_time(kMarket, 1'400'000'000LL, capital).accepted);
    assert(capital.snapshot().order_reserved_microdollars == 0);
}

void test_yes_no_buy_cycle_merge_releases_inventory_capital() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    assert(execution.process(
        quote_plan(3, kYes, pm::v7::Side::Buy, 48, 1'000'000), capital).accepted);
    assert(execution.on_public_trade(
        kMarket,
        trade(103, kYes, pm::v7::Side::Sell, 48, 1'000'000,
              10'100'000'000LL, 1'100'000'000LL), capital).accepted);
    assert(capital.snapshot().inventory_committed_microdollars == 480'000);

    assert(execution.process(
        quote_plan(4, kNo, pm::v7::Side::Buy, 49, 1'000'000,
                   1'200'000'000LL, 10'200'000'000LL), capital).accepted);
    const auto second = execution.on_public_trade(
        kMarket,
        trade(104, kNo, pm::v7::Side::Sell, 49, 1'000'000,
              10'300'000'000LL, 1'300'000'000LL), capital);
    assert(second.accepted);
    const auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 0);
    assert(snap.inventory_committed_microdollars == 0);
    assert(snap.total_exposure_microdollars == 0);
    const auto* inv = execution.inventory(kMarket);
    assert(inv->yes_microunits == 0 && inv->no_microunits == 0);
    assert(std::abs(inv->realized_trading_pnl - 0.03) < 1e-12);
}

void test_split_then_sell_releases_sold_leg_cost_basis() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    const auto split = execution.split_complete_sets(
        kMarket, 1'000'000, 900'000'000LL, 0.48, capital);
    assert(split.accepted);
    assert(capital.snapshot().inventory_committed_microdollars == 1'000'000);

    assert(execution.process(
        quote_plan(5, kYes, pm::v7::Side::Sell, 52, 1'000'000), capital).accepted);
    assert(execution.on_public_trade(
        kMarket,
        trade(105, kYes, pm::v7::Side::Buy, 52, 1'000'000,
              10'100'000'000LL, 1'100'000'000LL), capital).accepted);
    const auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 0);
    assert(snap.inventory_committed_microdollars == 520'000);
    assert(execution.inventory(kMarket)->yes_microunits == 0);
    assert(execution.inventory(kMarket)->no_microunits == 1'000'000);
    assert(std::abs(execution.inventory(kMarket)->realized_trading_pnl - 0.04) < 1e-12);
}

void test_unobservable_queue_fails_before_capital_or_paper_mutation() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    auto plan = quote_plan(6, kYes, pm::v7::Side::Buy, 48, 1'000'000);
    plan.public_queue_observable = 0;
    const auto result = execution.process(plan, capital);
    assert(!result.accepted);
    assert(result.reason == pm::v7::maker::MakerExecutionReason::QueueUnobservable);
    assert(capital.snapshot().total_exposure_microdollars == 0);
    assert(execution.quote_snapshot(kMarket, kYes).bid_active == 0);
}

void test_capital_denial_fails_before_paper_mutation() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits(400'000));
    const auto result = execution.process(
        quote_plan(7, kYes, pm::v7::Side::Buy, 48, 1'000'000), capital);
    assert(!result.accepted);
    assert(result.reason == pm::v7::maker::MakerExecutionReason::AdmissionDenied);
    assert(capital.snapshot().total_exposure_microdollars == 0);
    assert(execution.quote_snapshot(kMarket, kYes).bid_active == 0);
}

void test_duplicate_buy_plan_is_noop_not_double_reserved() {
    auto execution = policy();
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    const auto plan = quote_plan(8, kYes, pm::v7::Side::Buy, 48, 1'000'000);
    assert(execution.process(plan, capital).accepted);
    const auto duplicate = execution.process(plan, capital);
    assert(duplicate.accepted);
    assert(duplicate.reason == pm::v7::maker::MakerExecutionReason::DuplicateNoop);
    assert(capital.snapshot().order_reserved_microdollars == 480'000);
}

} // namespace

int main() {
    test_full_buy_fill_moves_reservation_to_inventory();
    test_partial_fill_preserves_total_capital_until_cancel_effective();
    test_control_plane_cancel_all_releases_buy_reservations();
    test_yes_no_buy_cycle_merge_releases_inventory_capital();
    test_split_then_sell_releases_sold_leg_cost_basis();
    test_unobservable_queue_fails_before_capital_or_paper_mutation();
    test_capital_denial_fails_before_paper_mutation();
    test_duplicate_buy_plan_is_noop_not_double_reserved();
    return 0;
}
