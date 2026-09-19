#include "pm/v7_crypto_decision_lane.hpp"

#include <atomic>
#include <cassert>
#include <cstdlib>
#include <new>

namespace {
std::atomic<std::uint64_t> allocations{0};
}

void* operator new(std::size_t size) {
    allocations.fetch_add(1, std::memory_order_relaxed);
    if (void* value = std::malloc(size)) return value;
    throw std::bad_alloc();
}
void operator delete(void* value) noexcept { std::free(value); }
void operator delete(void* value, std::size_t) noexcept { std::free(value); }

using namespace pm::v7;
using pm::v7::external_fair::ExternalCancelSignalSnapshot;

constexpr std::int64_t kNow = 1'000'000'000'000LL;

CapitalLimits limits() {
    return CapitalLimits{10'000'000, 10'000'000, 10'000'000, 5'000'000};
}
ExternalCancelSignalSnapshot signal(std::int8_t direction, std::uint64_t version = 1) {
    ExternalCancelSignalSnapshot value;
    value.signal_version = version;
    value.trigger_receive_monotonic_ns = kNow - 10'000'000;
    value.evaluated_grid_monotonic_ns = value.trigger_receive_monotonic_ns;
    value.valid_until_monotonic_ns = kNow + 90'000'000;
    value.binance_return_100ms_bp = direction > 0 ? 0.5 : -0.5;
    value.coinbase_return_100ms_bp = direction > 0 ? 0.2 : -0.2;
    value.direction = direction;
    value.confirmed_non_opposing = 1;
    value.valid = 1;
    return value;
}

BookHotSnapshot book(std::int32_t ask_e4, std::int64_t ask_size = 10'000'000) {
    BookHotSnapshot value;
    value.state_version = 9;
    value.exchange_event_ns = kNow - 2'000'000;
    value.receive_monotonic_ns = kNow - 1'000'000;
    value.tick_size_e4 = 100;
    value.best_bid_e4 = ask_e4 - 100;
    value.best_ask_e4 = ask_e4;
    value.best_bid_microunits = 10'000'000;
    value.best_ask_microunits = ask_size;
    value.bid_level_count = 1; value.ask_level_count = 1;
    value.bid_levels[0] = {ask_e4 - 100, 10'000'000};
    value.ask_levels[0] = {ask_e4, ask_size};
    value.lineage_continuous = 1; value.valid = 1;
    return value;
}
NativeCryptoMarketContext market() {
    NativeCryptoMarketContext value;
    value.market_handle = 7; value.event_handle = 8;
    value.close_monotonic_ns = kNow + 110'000'000'000LL;
    value.yes = {11, 1'000'000, 1, {}};
    value.no = {12, 1'000'000, 0, {}};
    value.accepting_orders = 1; value.contract_verified = 1;
    value.settlement_reference_valid = 1;
    return value;
}

NativeCryptoDecisionInput input(std::int8_t direction, std::uint64_t version = 1) {
    NativeCryptoDecisionInput value;
    value.signal = signal(direction, version);
    value.market = market();
    value.yes_book = book(4000);
    value.no_book = book(6000);
    value.now_monotonic_ns = kNow;
    value.model_version = 3; value.policy_version = 4;
    return value;
}

void test_up_down_and_admission() {
    NativeCryptoDecisionLane lane({});
    SleeveCapitalAccount capital(limits());
    auto up = lane.evaluate(input(1), capital);
    assert(up.accepted && up.reason == NativeCryptoDecisionReason::Accepted);
    assert(up.selected_yes && up.intent.instrument_handle == 11);
    assert(up.intent.strategy_id == StrategyId::CryptoInformedTaker);
    assert(up.intent.type == IntentType::TargetPosition && up.intent.side == Side::Buy);
    assert(up.intent.price_tick == 40 && up.intent.quantity_microunits == 5'000'000);
    assert(up.admission.reserved_microdollars == 2'000'000);
    assert(capital.release_order(up.intent.intent_id));
    lane.reset_market(7);
    auto down = lane.evaluate(input(-1, 2), capital);
    assert(down.accepted && !down.selected_yes && down.intent.instrument_handle == 12);
    assert(down.intent.price_tick == 60);
    assert(down.admission.reserved_microdollars == 3'000'000);
    assert(capital.release_order(down.intent.intent_id));
}

void test_duplicate_depth_tte_and_market_gates() {
    NativeCryptoDecisionLane lane({});
    SleeveCapitalAccount capital(limits());
    auto first = lane.evaluate(input(1), capital);
    assert(first.accepted); assert(capital.release_order(first.intent.intent_id));
    assert(lane.evaluate(input(1), capital).reason == NativeCryptoDecisionReason::DuplicateSignal);

    lane.reset_market(7);
    auto thin = input(1, 2); thin.yes_book.best_ask_microunits = 1'000'000;
    assert(lane.evaluate(thin, capital).reason == NativeCryptoDecisionReason::InsufficientDepth);
    auto stale = input(1, 3); stale.yes_book.receive_monotonic_ns = kNow - 200'000'000;
    assert(lane.evaluate(stale, capital).reason == NativeCryptoDecisionReason::InvalidBook);
    auto early = input(1, 4); early.market.close_monotonic_ns = kNow + 121'000'000'000LL;
    assert(lane.evaluate(early, capital).reason == NativeCryptoDecisionReason::TteOutsideWindow);
    auto closed = input(1, 5); closed.market.closed = 1;
    assert(lane.evaluate(closed, capital).reason == NativeCryptoDecisionReason::MarketUnavailable);
}
void test_entry_price_cap_rejects_without_consuming_signal() {
    NativeCryptoDecisionPolicy policy;
    policy.maximum_entry_price_e4 = 7'500;
    NativeCryptoDecisionLane lane(policy);
    SleeveCapitalAccount capital(limits());

    auto expensive = input(1, 30);
    expensive.yes_book = book(7'600);
    const auto rejected = lane.evaluate(expensive, capital);
    assert(rejected.accepted == 0);
    assert(rejected.reason == NativeCryptoDecisionReason::EntryPriceTooHigh);

    auto cheaper = input(1, 30);
    cheaper.yes_book = book(7'400);
    const auto accepted = lane.evaluate(cheaper, capital);
    assert(accepted.accepted == 1);
    assert(accepted.reason == NativeCryptoDecisionReason::Accepted);
    assert(accepted.intent.price_tick == 74);
    assert(capital.release_order(accepted.intent.intent_id));
}


void test_default_taker_keeps_baseline_shock_and_price_guard() {
    NativeCryptoDecisionLane lane({});
    SleeveCapitalAccount capital(limits());

    auto weak = input(1, 40);
    weak.signal.binance_return_100ms_bp = 0.29;
    assert(lane.evaluate(weak, capital).reason == NativeCryptoDecisionReason::WeakSignal);

    auto expensive = input(1, 41);
    expensive.yes_book = book(8'100);
    assert(lane.evaluate(expensive, capital).reason == NativeCryptoDecisionReason::EntryPriceTooHigh);

    auto admissible = input(1, 42);
    admissible.signal.binance_return_100ms_bp = 0.30;
    admissible.yes_book = book(8'000);
    const auto result = lane.evaluate(admissible, capital);
    assert(result.accepted == 1);
    assert(result.reason == NativeCryptoDecisionReason::Accepted);
    assert(result.intent.price_tick == 80);
    assert(capital.release_order(result.intent.intent_id));
}

void test_market_traded_and_capital_denied() {
    NativeCryptoDecisionLane lane({});
    SleeveCapitalAccount capital(limits());
    lane.mark_market_traded(7);
    assert(lane.evaluate(input(1), capital).reason == NativeCryptoDecisionReason::MarketAlreadyTraded);
    lane.reset_market(7);
    CapitalLimits small{1'000'000, 1'000'000, 1'000'000, 1'000'000};
    SleeveCapitalAccount constrained(small);
    auto result = lane.evaluate(input(1), constrained);
    assert(!result.accepted && result.reason == NativeCryptoDecisionReason::CapitalDenied);
}

void test_evaluate_is_allocation_free() {
    NativeCryptoDecisionLane lane({});
    SleeveCapitalAccount capital(limits());
    const auto before = allocations.load(std::memory_order_relaxed);
    auto result = lane.evaluate(input(1), capital);
    const auto after = allocations.load(std::memory_order_relaxed);
    assert(result.accepted);
    assert(after == before);
    assert(result.decision_compute_ns >= 0);
    assert(capital.release_order(result.intent.intent_id));
}

void test_construct_candidate_defers_capital_to_unified_owner() {
    NativeCryptoDecisionLane lane({});
    const auto result = lane.construct_candidate(input(1, 7));
    assert(result.accepted == 1);
    assert(result.reason == NativeCryptoDecisionReason::Accepted);
    assert(result.admission.accepted == 0);
    assert(result.admission.capital_reserved == 0);
}

void test_frozen_forward_can_ignore_source_valid_flag_with_age_gate() {
    NativeCryptoDecisionPolicy policy;
    policy.require_signal_valid = 0;
    NativeCryptoDecisionLane lane(policy);
    SleeveCapitalAccount capital(limits());
    auto value = input(1, 6);
    value.signal.valid = 0;
    value.signal.valid_until_monotonic_ns = value.now_monotonic_ns - 1;
    value.signal.trigger_receive_monotonic_ns = value.now_monotonic_ns - 4'000'000'000LL;
    const auto result = lane.evaluate(value, capital);
    assert(result.accepted == 1);
}

void test_runtime_policy_allows_twenty_shares_across_five_to_120_seconds() {
    NativeCryptoDecisionPolicy policy;
    policy.require_signal_valid = 0;
    policy.minimum_tte_ns = 5'000'000'000LL;
    policy.maximum_tte_ns = 120'000'000'000LL;
    policy.target_quantity_microunits = 20'000'000;
    NativeCryptoDecisionLane lane(policy);
    CapitalLimits larger{100'000'000, 100'000'000, 100'000'000, 25'000'000};
    SleeveCapitalAccount capital(larger);

    auto value = input(1, 20);
    value.market.close_monotonic_ns = kNow + 60'000'000'000LL;
    value.yes_book = book(4000, 25'000'000);
    const auto result = lane.evaluate(value, capital);
    assert(result.accepted == 1);
    assert(result.intent.quantity_microunits == 20'000'000);
    assert(result.admission.reserved_microdollars == 8'000'000);
    assert(capital.release_order(result.intent.intent_id));

    lane.reset_market(7);
    auto too_late = input(1, 21);
    too_late.market.close_monotonic_ns = kNow + 4'000'000'000LL;
    too_late.yes_book = book(4000, 25'000'000);
    assert(lane.evaluate(too_late, capital).reason == NativeCryptoDecisionReason::TteOutsideWindow);

    auto too_early = input(1, 22);
    too_early.market.close_monotonic_ns = kNow + 121'000'000'000LL;
    too_early.yes_book = book(4000, 25'000'000);
    assert(lane.evaluate(too_early, capital).reason == NativeCryptoDecisionReason::TteOutsideWindow);
}

void test_probability_selects_economic_side_not_signal_side() {
    NativeCryptoDecisionPolicy policy; policy.probability_ev_enabled=1;
    NativeCryptoDecisionLane lane(policy);
    auto x=input(1,80); // UP signal, but only DOWN is underpriced.
    x.probability={.up=.20,.lower=.15,.upper=.25,.asof_ns=kNow,
        .max_input_receive_ns=kNow-1,.valid_until_ns=kNow+1'000'000,
        .version=1,.valid=1};
    x.risk_sizing.allocated_wealth_microdollars=100'000'000;
    x.risk_sizing.available_microdollars=100'000'000;
    x.risk_sizing.maximum_chase_ticks=2;
    x.taker_fee_rate=.07; x.taker_fee_exponent=1; x.execution_reserve_per_share=.005;
    const auto before=allocations.load();
    auto d=lane.construct_candidate(x);
    assert(allocations.load()==before);
    assert(d.accepted && !d.selected_yes && d.intent.instrument_handle==12);
    assert(d.economics.conservative_net_edge>0);
    assert(d.economics.maximum_executable_price_e4==6200);
    assert(d.intent.price_tick==62);
    assert(d.economics.cost_ceiling_microdollars<=3'750'000);
    lane.reset_market(7);x.probability.valid=0;
    assert(lane.construct_candidate(x).reason==NativeCryptoDecisionReason::ProbabilityUnavailable);
    lane.reset_market(7);x.probability.valid=1;x.risk_sizing.max_order_cost_microdollars=100'000;
    assert(lane.construct_candidate(x).reason==NativeCryptoDecisionReason::RiskSizeBelowMinimum);
}

int main() {
    test_probability_selects_economic_side_not_signal_side();
    test_up_down_and_admission();
    test_duplicate_depth_tte_and_market_gates();
    test_entry_price_cap_rejects_without_consuming_signal();
    test_default_taker_keeps_baseline_shock_and_price_guard();
    test_market_traded_and_capital_denied();
    test_evaluate_is_allocation_free();
    test_construct_candidate_defers_capital_to_unified_owner();
    test_frozen_forward_can_ignore_source_valid_flag_with_age_gate();
    test_runtime_policy_allows_twenty_shares_across_five_to_120_seconds();
    return 0;
}
