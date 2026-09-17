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

int main() {
    test_up_down_and_admission();
    test_duplicate_depth_tte_and_market_gates();
    test_market_traded_and_capital_denied();
    test_evaluate_is_allocation_free();
    return 0;
}
