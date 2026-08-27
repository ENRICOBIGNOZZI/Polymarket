#include "pm/v7_maker_hft.hpp"
#include "pm/v7_spsc.hpp"

#include <cassert>
#include <chrono>
#include <memory>
#include <type_traits>

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
    model.base_quote_shares = 2.0;
    model.min_quote_lifetime_ns = 100'000'000;
    model.toxicity_withdraw_threshold = 0.95;
    model.one_sided_inventory_fraction = 0.50;
    return model;
}

pm::v7::maker::MarketUpdate normal_update() {
    pm::v7::maker::MarketUpdate update;
    update.market_handle = 11;
    update.event_handle = 22;
    update.instrument_handle = 33;
    update.state_version = 7;
    update.best_bid_tick = 48;
    update.best_ask_tick = 52;
    update.bid_depth_l1 = 20.0;
    update.ask_depth_l1 = 20.0;
    update.bid_depth_l5 = 100.0;
    update.ask_depth_l5 = 100.0;
    update.bid_depth_l10 = 150.0;
    update.ask_depth_l10 = 150.0;
    update.socket_receive_monotonic_ns = monotonic_ns();
    update.exchange_event_ns = 123456789;
    update.feed_healthy = 1;
    return update;
}

void test_strategy_intent_is_pod() {
    static_assert(std::is_trivially_copyable_v<pm::v7::StrategyIntent>);
    static_assert(std::is_standard_layout_v<pm::v7::StrategyIntent>);
}

void test_bounded_spsc_and_cancel_priority() {
    pm::v7::SpscRing<pm::v7::StrategyIntent, 4> ring;
    pm::v7::StrategyIntent x;
    x.intent_id = 1;
    assert(ring.try_push(x));
    x.intent_id = 2;
    assert(ring.try_push(x));
    x.intent_id = 3;
    assert(ring.try_push(x));
    x.intent_id = 4;
    assert(ring.try_push(x));
    x.intent_id = 5;
    assert(!ring.try_push(x));
    pm::v7::StrategyIntent out;
    assert(ring.try_pop(out) && out.intent_id == 1);

    pm::v7::PriorityIntentQueue<4, 4> queue;
    pm::v7::StrategyIntent quote;
    quote.intent_id = 10;
    quote.type = pm::v7::IntentType::Quote;
    pm::v7::StrategyIntent cancel;
    cancel.intent_id = 20;
    cancel.type = pm::v7::IntentType::CancelQuote;
    assert(queue.try_push(quote));
    assert(queue.try_push(cancel));
    assert(queue.try_pop(out));
    assert(out.intent_id == 20);
    assert(queue.try_pop(out));
    assert(out.intent_id == 10);
}

void test_atomic_model_snapshot() {
    auto first = std::make_shared<pm::v7::maker::MakerModelSnapshot>(profitable_model());
    first->model_version = 1;
    pm::v7::maker::MakerModelStore store(first);
    assert(store.snapshot()->model_version == 1);

    auto second = std::make_shared<pm::v7::maker::MakerModelSnapshot>(*first);
    second->model_version = 2;
    assert(store.publish(second));
    assert(store.snapshot()->model_version == 2);
}

void test_profitable_two_sided_post_only_quote() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    const auto model = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 3.0;
    risk.max_abs_residual_shares = 20.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::Quote);
    assert(decision.intent_count == 2);
    assert(decision.robust_ev > 0.0);
    bool saw_buy = false;
    bool saw_sell = false;
    for (std::size_t i = 0; i < decision.intent_count; ++i) {
        const auto& intent = decision.intents[i];
        assert(intent.type == pm::v7::IntentType::Quote);
        assert(intent.post_only == 1);
        assert(intent.passive == 1);
        assert(intent.model_version == model.model_version);
        assert(intent.state_version == update.state_version);
        if (intent.side == pm::v7::Side::Buy) {
            saw_buy = true;
            assert(intent.price_tick < update.best_ask_tick);
        } else if (intent.side == pm::v7::Side::Sell) {
            saw_sell = true;
            assert(intent.price_tick > update.best_bid_tick);
        }
    }
    assert(saw_buy && saw_sell);
    assert(decision.latency.feature_ns >= 0);
    assert(decision.latency.decision_ns >= 0);
    assert(decision.latency.receive_to_intent_ns >= 0);
}

void test_inventory_forces_reducing_side() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    const auto model = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 8.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 1.0;
    risk.max_abs_residual_shares = 10.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.action == pm::v7::maker::Action::OneSided);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Quote);
    assert(decision.intents[0].side == pm::v7::Side::Sell);
}

void test_global_kill_preempts_quote() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    const auto model = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.global_kill = 1;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::GlobalKill);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Kill);
    assert(decision.intents[0].urgency == pm::v7::Urgency::Critical);
}

} // namespace

int main() {
    test_strategy_intent_is_pod();
    test_bounded_spsc_and_cancel_priority();
    test_atomic_model_snapshot();
    test_profitable_two_sided_post_only_quote();
    test_inventory_forces_reducing_side();
    test_global_kill_preempts_quote();
    return 0;
}
