#include "pm/v7_maker_paper.hpp"

#include <cassert>
#include <cmath>

namespace {

constexpr std::uint64_t kMarket = 11;
constexpr std::uint64_t kYes = 101;
constexpr std::uint64_t kNo = 102;

pm::v7::StrategyIntent quote(std::uint64_t id, std::uint64_t instrument,
                             pm::v7::Side side, std::int64_t price_tick,
                             std::int64_t quantity, std::int64_t decision_ns,
                             std::int64_t exchange_ns) {
    pm::v7::StrategyIntent intent;
    intent.intent_id = id;
    intent.market_handle = kMarket;
    intent.event_handle = 22;
    intent.instrument_handle = instrument;
    intent.strategy_id = pm::v7::StrategyId::ProfessionalMaker;
    intent.type = pm::v7::IntentType::Quote;
    intent.side = side;
    intent.price_tick = price_tick;
    intent.quantity_microunits = quantity;
    intent.decision_monotonic_ns = decision_ns;
    intent.exchange_event_ns = exchange_ns;
    intent.passive = 1;
    intent.post_only = 1;
    return intent;
}

pm::v7::StrategyIntent cancel(std::uint64_t id, std::uint64_t instrument,
                              pm::v7::Side side, std::int64_t decision_ns,
                              std::int64_t exchange_ns) {
    auto intent = quote(id, instrument, side, 1, 1, decision_ns, exchange_ns);
    intent.type = pm::v7::IntentType::CancelQuote;
    intent.price_tick = 0;
    intent.quantity_microunits = 0;
    return intent;
}

pm::v7::StrategyIntent kill(std::uint64_t id, std::uint64_t instrument,
                            std::int64_t decision_ns, std::int64_t exchange_ns) {
    auto intent = cancel(id, instrument, pm::v7::Side::None, decision_ns, exchange_ns);
    intent.type = pm::v7::IntentType::Kill;
    return intent;
}

pm::v7::PublicTradePrint trade(std::uint64_t id, std::uint64_t instrument,
                               pm::v7::Side aggressor, std::int64_t price_tick,
                               std::int64_t quantity, std::int64_t exchange_ns,
                               std::int64_t receive_ns) {
    pm::v7::PublicTradePrint out;
    out.trade_id = id;
    out.instrument_handle = instrument;
    out.aggressor_side = aggressor;
    out.price_tick = price_tick;
    out.quantity_microunits = quantity;
    out.exchange_event_ns = exchange_ns;
    out.receive_monotonic_ns = receive_ns;
    return out;
}

const pm::v7::maker::PaperMakerEvent* find_event(
    const pm::v7::maker::PaperMakerResult& result,
    pm::v7::maker::PaperMakerEventKind kind) {
    for (std::size_t i = 0; i < result.event_count; ++i) {
        if (result.events[i].kind == kind) return &result.events[i];
    }
    return nullptr;
}

std::size_t first_event_index(const pm::v7::maker::PaperMakerResult& result,
                              pm::v7::maker::PaperMakerEventKind kind) {
    for (std::size_t i = 0; i < result.event_count; ++i) {
        if (result.events[i].kind == kind) return i;
    }
    return result.event_count;
}

void test_queue_envelope_and_pessimistic_operational_fill() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    const auto submitted = engine.apply_intent(
        quote(1, kYes, pm::v7::Side::Buy, 48, 1'000'000,
              1'000'000'000LL, 10'000'000'000LL),
        2'000'000, 100);
    assert(submitted.applied && !submitted.rejected);
    const auto* live = find_event(submitted, pm::v7::maker::PaperMakerEventKind::OrderLive);
    assert(live != nullptr);
    assert(live->queue.ahead_lower_microunits == 2'000'000);
    assert(live->queue.ahead_expected_microunits == 2'500'000);
    assert(live->queue.ahead_upper_microunits == 3'000'000);

    const auto first = engine.on_public_trade(trade(
        1001, kYes, pm::v7::Side::Sell, 48, 2'500'000,
        10'100'000'000LL, 1'100'000'000LL));
    const auto* diagnostic = find_event(first, pm::v7::maker::PaperMakerEventKind::Fill);
    assert(diagnostic != nullptr);
    assert(diagnostic->operational_fill_microunits == 0);
    assert(diagnostic->expected_fill_microunits == 0);
    assert(diagnostic->optimistic_fill_microunits == 500'000);
    assert(engine.inventory().yes_microunits == 0);

    const auto second = engine.on_public_trade(trade(
        1002, kYes, pm::v7::Side::Sell, 48, 1'500'000,
        10'200'000'000LL, 1'200'000'000LL));
    const auto* fill = find_event(second, pm::v7::maker::PaperMakerEventKind::Fill);
    assert(fill != nullptr);
    assert(fill->pessimistic_fill_microunits == 1'000'000);
    assert(fill->operational_fill_microunits == 1'000'000);
    assert(engine.inventory().yes_microunits == 1'000'000);
    assert(engine.active_order_count() == 0);
}

void test_public_print_is_idempotent() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    assert(engine.apply_intent(
        quote(1, kYes, pm::v7::Side::Buy, 48, 1'000'000,
              1'000'000'000LL, 10'000'000'000LL), 0, 100).applied);
    const auto print = trade(77, kYes, pm::v7::Side::Sell, 48, 500'000,
                             10'100'000'000LL, 1'100'000'000LL);
    assert(engine.on_public_trade(print).applied);
    const auto after_first = engine.inventory().yes_microunits;
    const auto duplicate = engine.on_public_trade(print);
    assert(duplicate.duplicate_trade);
    assert(engine.inventory().yes_microunits == after_first);
}

void test_cancel_pending_can_fill_until_effective_but_not_after() {
    pm::v7::maker::PaperMakerPolicy policy;
    policy.cancel_latency_ns = 100'000'000LL;
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo, policy);
    assert(engine.apply_intent(
        quote(1, kYes, pm::v7::Side::Buy, 48, 2'000'000,
              1'000'000'000LL, 10'000'000'000LL), 0, 100).applied);
    const auto cancellation = engine.apply_intent(
        cancel(2, kYes, pm::v7::Side::Buy, 1'200'000'000LL, 10'200'000'000LL), 0, 0);
    assert(find_event(cancellation, pm::v7::maker::PaperMakerEventKind::CancelRequested) != nullptr);

    const auto before = engine.on_public_trade(trade(
        88, kYes, pm::v7::Side::Sell, 48, 500'000,
        10'250'000'000LL, 1'250'000'000LL));
    const auto* fill = find_event(before, pm::v7::maker::PaperMakerEventKind::Fill);
    assert(fill != nullptr && fill->operational_fill_microunits == 500'000);
    assert(engine.inventory().yes_microunits == 500'000);

    const auto after = engine.on_public_trade(trade(
        89, kYes, pm::v7::Side::Sell, 48, 2'000'000,
        10'400'000'000LL, 1'400'000'000LL));
    assert(find_event(after, pm::v7::maker::PaperMakerEventKind::Fill) == nullptr);
    assert(engine.inventory().yes_microunits == 500'000);

    const auto advanced = engine.advance_time(1'400'000'000LL);
    assert(find_event(advanced, pm::v7::maker::PaperMakerEventKind::Cancelled) != nullptr);
    assert(engine.active_order_count() == 0);
}

void test_yes_no_buys_merge_complete_set_and_realize_trading_pnl() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    assert(engine.apply_intent(
        quote(1, kYes, pm::v7::Side::Buy, 48, 1'000'000,
              1'000'000'000LL, 10'000'000'000LL), 0, 100).applied);
    assert(engine.on_public_trade(trade(
        91, kYes, pm::v7::Side::Sell, 48, 1'000'000,
        10'100'000'000LL, 1'100'000'000LL)).applied);

    assert(engine.apply_intent(
        quote(2, kNo, pm::v7::Side::Buy, 49, 1'000'000,
              1'200'000'000LL, 10'200'000'000LL), 0, 100).applied);
    const auto result = engine.on_public_trade(trade(
        92, kNo, pm::v7::Side::Sell, 49, 1'000'000,
        10'300'000'000LL, 1'300'000'000LL));
    const auto* merged = find_event(result, pm::v7::maker::PaperMakerEventKind::FinalMerge);
    assert(merged != nullptr);
    assert(merged->operational_fill_microunits == 1'000'000);
    assert(std::abs(merged->realized_pnl - 0.03) < 1e-12);
    assert(first_event_index(result, pm::v7::maker::PaperMakerEventKind::Fill)
           < first_event_index(result, pm::v7::maker::PaperMakerEventKind::FinalMerge));
    assert(engine.inventory().yes_microunits == 0);
    assert(engine.inventory().no_microunits == 0);
    assert(std::abs(engine.inventory().realized_trading_pnl - 0.03) < 1e-12);
}

void test_merge_does_not_consume_inventory_reserved_by_live_sell() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    assert(engine.apply_intent(
        quote(1, kYes, pm::v7::Side::Buy, 48, 2'000'000,
              1'000'000'000LL, 10'000'000'000LL), 0, 100).applied);
    assert(engine.on_public_trade(trade(
        201, kYes, pm::v7::Side::Sell, 48, 2'000'000,
        10'100'000'000LL, 1'100'000'000LL)).applied);
    assert(engine.inventory().yes_microunits == 2'000'000);

    assert(engine.apply_intent(
        quote(2, kYes, pm::v7::Side::Sell, 52, 1'000'000,
              1'200'000'000LL, 10'200'000'000LL), 0, 100).applied);
    assert(engine.apply_intent(
        quote(3, kNo, pm::v7::Side::Buy, 49, 2'000'000,
              1'300'000'000LL, 10'300'000'000LL), 0, 100).applied);
    const auto result = engine.on_public_trade(trade(
        202, kNo, pm::v7::Side::Sell, 49, 2'000'000,
        10'400'000'000LL, 1'400'000'000LL));
    const auto* merged = find_event(result, pm::v7::maker::PaperMakerEventKind::FinalMerge);
    assert(merged != nullptr);
    assert(merged->operational_fill_microunits == 1'000'000);
    assert(engine.inventory().yes_microunits == 1'000'000);
    assert(engine.inventory().no_microunits == 1'000'000);
    assert(engine.active_order_count() == 1);
}

void test_market_kill_cancels_both_yes_and_no_quotes() {
    pm::v7::maker::PaperMakerPolicy policy;
    policy.cancel_latency_ns = 10;
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo, policy);
    assert(engine.apply_intent(
        quote(1, kYes, pm::v7::Side::Buy, 48, 1'000'000,
              1'000'000'000LL, 10'000'000'000LL), 0, 100).applied);
    assert(engine.apply_intent(
        quote(2, kNo, pm::v7::Side::Buy, 48, 1'000'000,
              1'100'000'000LL, 10'100'000'000LL), 0, 100).applied);
    assert(engine.active_order_count() == 2);
    const auto killed = engine.apply_intent(
        kill(3, kYes, 1'200'000'000LL, 10'200'000'000LL), 0, 0);
    assert(killed.applied);
    assert(killed.event_count == 2);
    const auto advanced = engine.advance_time(1'200'000'020LL);
    std::size_t cancelled = 0;
    for (std::size_t i = 0; i < advanced.event_count; ++i) {
        if (advanced.events[i].kind == pm::v7::maker::PaperMakerEventKind::Cancelled) ++cancelled;
    }
    assert(cancelled == 2);
    assert(engine.active_order_count() == 0);
}

void test_uncovered_sell_is_rejected() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    const auto result = engine.apply_intent(
        quote(1, kYes, pm::v7::Side::Sell, 52, 1'000'000,
              1'000'000'000LL, 10'000'000'000LL), 0, 100);
    assert(result.rejected);
    assert(engine.active_order_count() == 0);
}

void test_inventory_snapshot_reservations_have_global_yes_minus_no_sign() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    assert(engine.apply_intent(
        quote(1, kNo, pm::v7::Side::Buy, 48, 1'000'000,
              1'000'000'000LL, 10'000'000'000LL), 0, 100).applied);
    const auto inventory = engine.inventory_snapshot();
    assert(inventory.residual_shares() < -0.99);
}

} // namespace

int main() {
    test_queue_envelope_and_pessimistic_operational_fill();
    test_public_print_is_idempotent();
    test_cancel_pending_can_fill_until_effective_but_not_after();
    test_yes_no_buys_merge_complete_set_and_realize_trading_pnl();
    test_merge_does_not_consume_inventory_reserved_by_live_sell();
    test_market_kill_cancels_both_yes_and_no_quotes();
    test_uncovered_sell_is_rejected();
    test_inventory_snapshot_reservations_have_global_yes_minus_no_sign();
    return 0;
}
