#include "pm/v7_latency_arb.hpp"

#include <cassert>
#include <iostream>

using namespace pm::v7;

BookHotSnapshot book(std::int32_t bid, std::int32_t ask,
                     std::int64_t receive_ns, std::uint64_t version = 1) {
    BookHotSnapshot b{};
    b.valid = 1;
    b.lineage_continuous = 1;
    b.state_version = version;
    b.tick_size_e4 = 100;
    b.best_bid_e4 = bid;
    b.best_ask_e4 = ask;
    b.best_bid_microunits = 20'000'000;
    b.best_ask_microunits = 20'000'000;
    b.receive_monotonic_ns = receive_ns;
    b.exchange_event_ns = receive_ns - 1000;
    return b;
}

leadlag::EventLeadLagSignal signal(std::uint64_t version, std::int8_t direction,
                                   std::int64_t trigger_ns) {
    leadlag::EventLeadLagSignal s{};
    s.signal_version = version;
    s.causal_trigger_receive_monotonic_ns = trigger_ns;
    s.evaluated_receive_monotonic_ns = trigger_ns;
    s.valid_until_monotonic_ns = trigger_ns + 100'000'000;
    s.binance_return_100ms_bp = direction > 0 ? 0.50 : -0.50;
    s.coinbase_return_100ms_bp = direction > 0 ? 0.10 : -0.10;
    s.direction = direction;
    s.confirmed_non_opposing = 1;
    s.valid = 1;
    return s;
}

NativeCryptoMarketContext market(std::int64_t close_ns) {
    NativeCryptoMarketContext m{};
    m.market_handle = 1;
    m.event_handle = 1;
    m.close_monotonic_ns = close_ns;
    m.yes = {1, 5'000'000, 1, {}};
    m.no = {2, 5'000'000, 0, {}};
    m.accepting_orders = 1;
    m.contract_verified = 1;
    m.settlement_reference_valid = 1;
    return m;
}

int main() {
    LatencyArbPolicy policy{};
    policy.minimum_tte_ns = 1'000'000'000;
    policy.maximum_tte_ns = 300'000'000'000;
    policy.hard_timeout_ns = 500'000'000;
    policy.maximum_spread_ticks = 2;
    policy.minimum_profit_ticks = 1;
    LatencyArbLane lane(policy);

    const std::int64_t trigger = 10'000'000'000LL;
    LatencyArbInput in{};
    in.signal = signal(1, 1, trigger);
    in.market = market(trigger + 60'000'000'000LL);
    in.yes_book = book(4500, 4600, trigger - 1'000'000);
    in.no_book = book(5300, 5400, trigger - 1'000'000);
    in.now_monotonic_ns = trigger + 100'000;
    in.taker_fee_rate = 0.0;
    in.taker_fee_exponent = 1.0;

    auto enter = lane.construct_candidate(in);
    assert(enter.accepted == 1);
    assert(enter.action == LatencyArbAction::Enter);
    assert(enter.intent.side == Side::Buy);
    assert(enter.selected_instrument_handle == 1);
    assert(enter.entry_price_e4 == 4600);
    assert(enter.target_exit_e4 == 4700);
    lane.on_submitted(100, enter);
    lane.on_fill(100, 1, Side::Buy, 5'000'000, 4600, trigger + 2'000'000);
    lane.on_terminal(100);
    assert(lane.position_open());

    in.yes_book = book(4700, 4800, trigger + 10'000'000, 2);
    in.now_monotonic_ns = trigger + 10'100'000;
    auto exit = lane.construct_candidate(in);
    assert(exit.accepted == 1);
    assert(exit.reason == LatencyArbReason::ExitConverged);
    assert(exit.intent.side == Side::Sell);
    lane.on_submitted(101, exit);
    lane.on_fill(101, 1, Side::Sell, 5'000'000, 4700, trigger + 12'000'000);
    lane.on_terminal(101);
    assert(!lane.position_open());

    in.signal = signal(2, -1, trigger + 20'000'000);
    in.no_book = book(5300, 5400, trigger + 19'000'000, 3);
    in.yes_book = book(4500, 4600, trigger + 19'000'000, 3);
    in.now_monotonic_ns = trigger + 20'100'000;
    auto down = lane.construct_candidate(in);
    assert(down.accepted == 1);
    assert(down.selected_instrument_handle == 2);
    assert(down.intent.side == Side::Buy);

    LatencyArbLane stale(policy);
    auto post = in;
    post.signal = signal(3, 1, trigger + 30'000'000);
    post.yes_book = book(4600, 4700, trigger + 30'000'001, 4);
    post.now_monotonic_ns = trigger + 30'100'000;
    auto rejected = stale.construct_candidate(post);
    assert(rejected.accepted == 0);
    assert(rejected.reason == LatencyArbReason::BookAlreadyRepriced);

    std::cout << "latency arb tests passed\n";
    return 0;
}
