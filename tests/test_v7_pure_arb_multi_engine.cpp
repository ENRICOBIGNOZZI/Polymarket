#include "pm/v7_pure_arb_multi_engine.hpp"

#include <array>
#include <cassert>
#include <cmath>
#include <filesystem>

using namespace pm::v7;
using namespace pm::v7::pure_arb;

namespace {
BookHotSnapshot book(bool yes, std::int64_t receive_ns, std::uint64_t version) {
    BookHotSnapshot b{};
    b.valid = 1;
    b.lineage_continuous = 1;
    b.tick_size_e4 = 100;
    b.state_version = version;
    b.receive_monotonic_ns = receive_ns;
    b.exchange_event_ns = receive_ns - 100;
    b.best_bid_e4 = yes ? 3900 : 4900;
    b.best_ask_e4 = yes ? 4000 : 5000;
    b.best_bid_microunits = 10'000'000;
    b.best_ask_microunits = 10'000'000;
    b.bid_levels[0] = {b.best_bid_e4, 10'000'000};
    b.ask_levels[0] = {b.best_ask_e4, 10'000'000};
    b.bid_level_count = 1;
    b.ask_level_count = 1;
    return b;
}

MarketWsEvent event(std::uint64_t market, std::uint64_t event_handle,
                    std::uint64_t instrument, BookHotSnapshot b,
                    std::int64_t decode_ns) {
    MarketWsEvent e{};
    e.kind = MarketWsEventKind::BookChanged;
    e.market_handle = market;
    e.event_handle = event_handle;
    e.instrument_handle = instrument;
    e.state_version = b.state_version;
    e.exchange_event_ns = b.exchange_event_ns;
    e.receive_monotonic_ns = b.receive_monotonic_ns;
    e.decode_complete_monotonic_ns = decode_ns;
    e.book = b;
    return e;
}
}

int main() {
    std::array<MultiMarketConfig, 30> configs{};
    for (std::size_t i = 0; i < configs.size(); ++i) {
        configs[i].market_handle = 100 + i;
        configs[i].event_handle = 200 + i;
        configs[i].yes_instrument_handle = 1 + 2 * i;
        configs[i].no_instrument_handle = 2 + 2 * i;
        configs[i].market_start_wall_ms = 1'000;
        configs[i].market_end_wall_ms = 2'000;
        configs[i].maximum_leg_skew_ns = 1'000;
        configs[i].minimum_order_microunits = 5'000'000;
        configs[i].fee_rate = 0.0;
        configs[i].fee_exponent = 1.0;
        configs[i].reserve_per_share = 0.0005;
        configs[i].fee_verified = 1;
    }

    CapitalLimits limits{};
    limits.sleeve_budget_microdollars = 100'000'000;
    limits.max_total_exposure_microdollars = 100'000'000;
    limits.max_market_exposure_microdollars = 20'000'000;
    limits.max_single_order_microdollars = 10'000'000;
    NativeSettlementAuthority authority(limits);

    const auto path = std::filesystem::temp_directory_path()
        / "pm-v7-pure-arb-multi-engine-latency.bin";
    std::error_code ec;
    std::filesystem::remove(path, ec);
    NativeLatencyTape tape(path.string());

    MultiMarketEngine engine(configs, authority, &tape);
    assert(engine.valid());
    assert(engine.market_count() == 30);

    auto yes_book = book(true, 10'000, 1);
    auto no_book = book(false, 10'100, 1);
    auto yes_event = event(100, 200, 1, yes_book, 10'050);
    auto no_event = event(100, 200, 2, no_book, 10'150);

    const auto first = engine.on_market_event(yes_event, 7, 1'500);
    assert(first.evaluated == 1);
    assert(first.admitted == 0);

    const auto second = engine.on_market_event(no_event, 7, 1'500);
    assert(second.evaluated == 1);
    assert(second.admitted == 1);
    assert(second.plan.accepted == 1);
    assert(second.plan.direction == Direction::BuyCompleteSet);
    assert(second.admission.accepted == 1);
    assert(second.admission.yes.tx.command.time_in_force == AdapterTimeInForce::Fok);
    assert(second.admission.no.tx.command.time_in_force == AdapterTimeInForce::Fok);
    assert(second.admission.yes.tx.command.instrument_handle == 1);
    assert(second.admission.no.tx.command.instrument_handle == 2);
    assert(second.admission.yes.tx.command.quantity_microunits
           == second.admission.no.tx.command.quantity_microunits);
    assert(authority.active_orders() == 2);

    PairInput direct{};
    direct.market_handle = 100;
    direct.event_handle = 200;
    direct.yes_instrument_handle = 1;
    direct.no_instrument_handle = 2;
    direct.yes_epoch = direct.no_epoch = 7;
    direct.market_start_wall_ms = 1'000;
    direct.market_end_wall_ms = 2'000;
    direct.now_wall_ms = 1'500;
    direct.trigger_receive_monotonic_ns = 10'100;
    direct.decode_complete_monotonic_ns = 10'150;
    direct.maximum_leg_skew_ns = 1'000;
    direct.minimum_order_microunits = 5'000'000;
    direct.sell_available_microunits = 0;
    direct.fee_rate = 0.0;
    direct.fee_exponent = 1.0;
    direct.reserve_per_share = 0.0005;
    direct.fee_verified = 1;
    direct.yes = yes_book;
    direct.no = no_book;
    const auto expected = evaluate_pair(direct);
    assert(expected.accepted == 1);
    assert(expected.economics.shares_microunits
           == second.plan.economics.shares_microunits);
    assert(expected.yes.limit_price_e4 == second.plan.yes.limit_price_e4);
    assert(expected.no.limit_price_e4 == second.plan.no.limit_price_e4);
    assert(std::abs(expected.economics.gross_locked_pnl
                    - second.plan.economics.gross_locked_pnl) < 1e-12);

    tape.stop();
    const auto snapshot = tape.snapshot();
    assert(snapshot.dropped == 0);
    assert(snapshot.published == 8);
    assert(snapshot.written == 8);
    assert(std::filesystem::file_size(path)
           == 8 * sizeof(NativeLatencyEvent));

    OmsEvent reject{};
    reject.type = OmsEventType::Reject;
    reject.timestamp_ns = second.admission.risk_admitted_monotonic_ns + 1;
    const auto y = authority.apply_order_event(
        second.admission.yes.tx.command.client_order_id, reject);
    const auto n = authority.apply_order_event(
        second.admission.no.tx.command.client_order_id, reject);
    assert(y.terminal_retired && n.terminal_retired);
    assert(authority.active_orders() == 0);

    engine.invalidate_all();
    auto after = engine.on_market_event(no_event, 8, 1'500);
    assert(after.evaluated == 1);
    assert(after.admitted == 0);

    std::filesystem::remove(path, ec);
    return 0;
}
