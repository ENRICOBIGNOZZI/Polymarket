#include "pm/v7_maker_hft.hpp"
#include "pm/v7_spsc.hpp"

#include <cassert>
#include <chrono>
#include <cmath>
#include <memory>
#include <type_traits>

namespace {

std::int64_t monotonic_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

double logit(double p) {
    return std::log(p / (1.0 - p));
}

pm::v7::maker::MakerModelSnapshot profitable_model() {
    pm::v7::maker::MakerModelSnapshot model;
    model.tick_size = 0.01;
    model.fill_coefficients.fill(0.0);
    model.fill_coefficients[0] = 3.0;
    model.markout_coefficients.fill(0.0);
    model.base_ev_se_per_share = 0.0;
    model.robust_ev_z = 0.0;
    model.exploration_confidence_z = 0.0;
    model.inventory_cost_per_share = 0.0;
    model.latency_cost_per_second = 0.0;
    model.base_quote_shares = 2.0;
    model.min_quote_lifetime_ns = 100'000'000;
    model.toxicity_withdraw_threshold = 0.95;
    model.one_sided_inventory_fraction = 0.50;
    for (auto& cell : model.execution_cells) cell = {};
    return model;
}

pm::v7::maker::MakerModelSnapshot cell_model() {
    auto model = profitable_model();
    model.fill_coefficients.fill(0.0);
    model.fill_coefficients[0] = logit(0.01);
    model.markout_coefficients.fill(0.0);
    model.markout_coefficients[0] = 0.05;
    model.base_quote_shares = 1.0;
    for (auto& cell : model.execution_cells) {
        cell.fill_probability = 0.01;
        cell.adverse_markout_per_share = 0.05;
        cell.fill_weight = 1.0;
        cell.markout_weight = 1.0;
        cell.orders = 100;
        cell.filled_orders = 20;
        cell.adverse_markouts = 20;
        cell.event_clusters = 20;
        cell.valid = 1;
    }
    return model;
}

void make_cell_good(pm::v7::maker::MakerModelSnapshot& model,
                    pm::v7::maker::Action action,
                    std::int8_t outcome_sign,
                    pm::v7::Side side) {
    const auto index = pm::v7::maker::execution_cell_index(action, outcome_sign, side);
    assert(index < model.execution_cells.size());
    auto& cell = model.execution_cells[index];
    cell.fill_probability = 0.90;
    cell.adverse_markout_per_share = 0.0;
    cell.fill_weight = 1.0;
    cell.markout_weight = 1.0;
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
    update.instrument_inventory_sign = 1;
    update.feed_healthy = 1;
    return update;
}

void test_strategy_intent_is_pod() {
    static_assert(std::is_trivially_copyable_v<pm::v7::StrategyIntent>);
    static_assert(std::is_standard_layout_v<pm::v7::StrategyIntent>);
}

void test_execution_cell_index_is_action_outcome_side_specific() {
    using pm::v7::Side;
    using pm::v7::maker::Action;
    using pm::v7::maker::execution_cell_index;
    assert(execution_cell_index(Action::Join, 1, Side::Buy) == 0);
    assert(execution_cell_index(Action::Join, 1, Side::Sell) == 1);
    assert(execution_cell_index(Action::Join, -1, Side::Buy) == 2);
    assert(execution_cell_index(Action::Join, -1, Side::Sell) == 3);
    assert(execution_cell_index(Action::Improve1, 1, Side::Buy) == 4);
    assert(execution_cell_index(Action::Fade2, -1, Side::Sell) == 15);
    assert(execution_cell_index(Action::OneSided, 1, Side::Buy)
           == pm::v7::maker::kExecutionCellCount);
    assert(execution_cell_index(Action::Join, 0, Side::Buy)
           == pm::v7::maker::kExecutionCellCount);
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
    inventory.yes_shares = 3.0;
    inventory.no_shares = 3.0;
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

void test_authoritative_zero_flow_blocks_passive_quote() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    update.flow_prior_valid = 1;
    const auto model = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 3.0;
    inventory.no_shares = 3.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 3.0;
    risk.max_abs_residual_shares = 20.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::NoEconomicQuote);
    assert(decision.bid_fill_probability == 0.0);
    assert(decision.ask_fill_probability == 0.0);
}

void test_side_specific_opposite_flow_authorizes_only_reachable_side() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    update.flow_prior_valid = 1;
    // Aggressive SELLs can reach our BUY.  No aggressive BUY flow means our
    // inventory-backed SELL is not an executable opportunity.
    update.prior_aggressive_sell_shares_per_second = 100.0;
    const auto model = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 3.0;
    inventory.no_shares = 3.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 3.0;
    risk.max_abs_residual_shares = 20.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::Quote);
    assert(decision.action == pm::v7::maker::Action::OneSided);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].side == pm::v7::Side::Buy);
    assert(decision.bid_flow_reach_probability > 0.90);
    assert(decision.ask_flow_reach_probability == 0.0);
}

void test_trade_rate_uses_elapsed_time_not_book_message_count() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    update.flow_prior_valid = 1;
    update.prior_aggressive_sell_shares_per_second = 10.0;
    const auto model = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 3.0;
    risk.max_abs_residual_shares = 20.0;

    const auto first = hot.on_market_update(update, inventory, quotes, risk, model);
    const double seeded_rate = first.features.aggressive_sell_shares_per_second;
    assert(std::abs(seeded_rate - 10.0) < 1e-12);
    for (std::uint64_t version = 8; version < 108; ++version) {
        update.state_version = version;
        const auto next = hot.on_market_update(update, inventory, quotes, risk, model);
        assert(std::abs(next.features.aggressive_sell_shares_per_second - seeded_rate) < 1e-12);
    }
}

void test_specific_improve_cell_changes_candidate_ranking() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto model = cell_model();
    make_cell_good(model, pm::v7::maker::Action::Improve1, 1, pm::v7::Side::Buy);
    make_cell_good(model, pm::v7::maker::Action::Improve1, 1, pm::v7::Side::Sell);
    assert(model.valid());

    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 1.0;
    inventory.no_shares = 1.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 1.0;
    risk.max_abs_residual_shares = 20.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::Quote);
    assert(decision.action == pm::v7::maker::Action::Improve1);
    assert(decision.intent_count == 2);
    bool saw_49 = false;
    bool saw_51 = false;
    for (std::size_t i = 0; i < decision.intent_count; ++i) {
        saw_49 = saw_49 || (decision.intents[i].side == pm::v7::Side::Buy
                            && decision.intents[i].price_tick == 49);
        saw_51 = saw_51 || (decision.intents[i].side == pm::v7::Side::Sell
                            && decision.intents[i].price_tick == 51);
    }
    assert(saw_49 && saw_51);
}

void test_settlement_fair_uses_lower_for_bid_and_upper_for_ask() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    update.related_state_valid = 1;
    update.related_fair_value = 0.52;
    update.related_fair_lower = 0.51;
    update.related_fair_upper = 0.56;
    update.related_snapshot_age_ns = 1'000'000;
    auto model = profitable_model();
    model.mid_weight = 0.0;
    model.microprice_weight = 0.0;
    model.flow_weight = 0.0;
    model.related_weight = 1.0;
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 3.0;
    inventory.no_shares = 3.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 20.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.external_fair_authority == 1);
    assert(std::abs(decision.fair_value - 0.52) < 1e-12);
    assert(std::abs(decision.fair_lower - 0.51) < 1e-12);
    assert(std::abs(decision.fair_upper - 0.56) < 1e-12);
    assert(decision.reason == pm::v7::maker::DecisionReason::Quote);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].side == pm::v7::Side::Buy);
}

void test_outcome_cell_orientation_is_respected() {
    auto model = cell_model();
    make_cell_good(model, pm::v7::maker::Action::Improve1, -1, pm::v7::Side::Buy);
    make_cell_good(model, pm::v7::maker::Action::Improve1, -1, pm::v7::Side::Sell);
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 1.0;
    inventory.no_shares = 1.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 1.0;
    risk.max_abs_residual_shares = 20.0;

    auto yes_update = normal_update();
    yes_update.instrument_inventory_sign = 1;
    pm::v7::maker::MakerHotPath yes_hot;
    const auto yes = yes_hot.on_market_update(yes_update, inventory, quotes, risk, model);
    assert(yes.reason == pm::v7::maker::DecisionReason::NoEconomicQuote);

    auto no_update = normal_update();
    no_update.instrument_inventory_sign = -1;
    pm::v7::maker::MakerHotPath no_hot;
    const auto no = no_hot.on_market_update(no_update, inventory, quotes, risk, model);
    assert(no.reason == pm::v7::maker::DecisionReason::Quote);
    assert(no.action == pm::v7::maker::Action::Improve1);
}

void test_sell_cell_controls_one_sided_inventory_reduction() {
    auto model = cell_model();
    make_cell_good(model, pm::v7::maker::Action::Improve1, 1, pm::v7::Side::Sell);
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 8.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 1.0;
    risk.max_abs_residual_shares = 10.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.action == pm::v7::maker::Action::OneSided);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].side == pm::v7::Side::Sell);
    assert(decision.intents[0].price_tick == 51);
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

void test_partial_inventory_sizes_reducing_quote_to_available_residual() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto model = profitable_model();
    model.base_quote_shares = 20.0;
    model.one_sided_inventory_fraction = 0.01;
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 10.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.max_abs_residual_shares = 200.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.action == pm::v7::maker::Action::OneSided);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].side == pm::v7::Side::Sell);
    assert(decision.intents[0].quantity_microunits == 10'000'000);
}

void test_transient_no_economic_quote_cannot_cancel_before_minimum_lifetime() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto profitable = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    quotes.bid_active = 1;
    quotes.ask_active = 1;
    quotes.bid_tick = update.best_bid_tick;
    quotes.ask_tick = update.best_ask_tick;
    quotes.last_quote_monotonic_ns = monotonic_ns();
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 20.0;

    auto unprofitable = profitable;
    unprofitable.markout_coefficients.fill(1.0);
    unprofitable.min_quote_lifetime_ns = 1'000'000'000LL;
    const auto held = hot.on_market_update(update, inventory, quotes, risk, unprofitable);
    assert(held.reason == pm::v7::maker::DecisionReason::QuoteLifetimeHold);
    assert(held.intent_count == 0);

    quotes.last_quote_monotonic_ns = monotonic_ns() - 2'000'000'000LL;
    const auto cancelled = hot.on_market_update(update, inventory, quotes, risk, unprofitable);
    assert(cancelled.reason == pm::v7::maker::DecisionReason::NoEconomicQuote);
    assert(cancelled.intent_count == 1);
    assert(cancelled.intents[0].type == pm::v7::IntentType::Withdraw);
}

void configure_exploration(pm::v7::maker::MakerModelSnapshot& model) {
    model.exploration_confidence_z = 0.5;
    model.exploration_enabled = 1;
    model.exploration_epsilon = 1.0;
    model.exploration_quote_notional_fraction = 0.002;
    model.exploration_max_active_markets = 20;
    model.exploration_concurrent_market_cap = 5;
    model.exploration_min_rest_ns = 3'000'000'000LL;
    model.exploration_max_rest_ns = 15'000'000'000LL;
    model.exploration_persistent_max_rest_ns = model.exploration_max_rest_ns;
}

void test_positive_exploration_ev_breaks_robust_cold_start() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto model = profitable_model();
    // Keep every normal JOIN/IMPROVE/FADE candidate below the 1.64-sigma
    // exploit gate while leaving the touch JOIN above the 0.5-sigma PAPER
    // exploration gate.
    model.base_ev_se_per_share = 0.025;
    model.robust_ev_z = 1.64;
    model.min_robust_ev_per_share = 0.00001;
    configure_exploration(model);
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::ExplorationQuote);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Quote);
    assert(decision.intents[0].expected_ev > model.min_robust_ev_per_share);
    assert(decision.intents[0].quantity_microunits == 2'000'000);
}

void test_point_ev_exploration_prefers_highest_economic_placement() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto model = profitable_model();
    model.base_ev_se_per_share = 0.025;
    model.robust_ev_z = 100.0;
    model.min_robust_ev_per_share = 0.00001;
    configure_exploration(model);
    model.exploration_confidence_z = 0.0;
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::ExplorationQuote);
    assert(decision.action == pm::v7::maker::Action::Join);
    assert(decision.intent_count == 1);
    if (decision.intents[0].side == pm::v7::Side::Buy) {
        assert(decision.intents[0].price_tick == update.best_bid_tick);
    } else {
        assert(decision.intents[0].side == pm::v7::Side::Sell);
        assert(decision.intents[0].price_tick == update.best_ask_tick);
    }
    assert(decision.intents[0].expected_ev > model.min_robust_ev_per_share);
}

void test_exploration_collects_positive_buy_and_inventory_backed_sell_evidence() {
    auto model = profitable_model();
    model.base_ev_se_per_share = 0.025;
    model.robust_ev_z = 100.0;
    model.min_robust_ev_per_share = 0.00001;
    configure_exploration(model);
    model.exploration_confidence_z = 0.0;
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 10.0;
    inventory.no_shares = 10.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    bool saw_buy = false;
    bool saw_sell = false;
    for (std::uint64_t version = 1; version <= 128 && !(saw_buy && saw_sell); ++version) {
        pm::v7::maker::MakerHotPath hot;
        auto update = normal_update();
        update.state_version = version;
        const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
        assert(decision.reason == pm::v7::maker::DecisionReason::ExplorationQuote);
        assert(decision.intent_count == 1);
        assert(decision.intents[0].expected_ev > model.min_robust_ev_per_share);
        saw_buy = saw_buy || decision.intents[0].side == pm::v7::Side::Buy;
        saw_sell = saw_sell || decision.intents[0].side == pm::v7::Side::Sell;
    }
    assert(saw_buy && saw_sell);
}

void test_exploration_never_emits_uncovered_sell() {
    auto model = profitable_model();
    model.base_ev_se_per_share = 0.025;
    model.robust_ev_z = 100.0;
    model.min_robust_ev_per_share = 0.00001;
    configure_exploration(model);
    model.exploration_confidence_z = 0.0;
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    for (std::uint64_t version = 1; version <= 128; ++version) {
        pm::v7::maker::MakerHotPath hot;
        auto update = normal_update();
        update.state_version = version;
        const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
        assert(decision.reason == pm::v7::maker::DecisionReason::ExplorationQuote);
        assert(decision.intent_count == 1);
        assert(decision.intents[0].side == pm::v7::Side::Buy);
    }
}

void test_persistent_lifetime_arm_is_explicit_and_bounded() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto model = profitable_model();
    model.base_ev_se_per_share = 0.025;
    model.robust_ev_z = 100.0;
    model.min_robust_ev_per_share = 0.00001;
    configure_exploration(model);
    model.exploration_confidence_z = 0.0;
    model.exploration_persistent_fraction = 1.0;
    model.exploration_persistent_max_rest_ns = 60'000'000'000LL;
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::ExplorationQuote);
    assert(decision.exploration_max_rest_ns == 60'000'000'000LL);
    assert(decision.exploration_persistent == 1);
    assert(decision.intent_count == 1);
}

void test_exploration_minimum_rest_survives_transient_negative_ev() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto model = profitable_model();
    model.base_ev_se_per_share = 0.025;
    model.robust_ev_z = 100.0;
    model.min_robust_ev_per_share = 0.00001;
    model.min_quote_lifetime_ns = 0;
    configure_exploration(model);
    model.exploration_confidence_z = 0.0;
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 10.0;
    inventory.no_shares = 10.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    const auto opened = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(opened.reason == pm::v7::maker::DecisionReason::ExplorationQuote);
    assert(opened.intent_count == 1);
    const auto& quote = opened.intents[0];
    quotes.last_quote_monotonic_ns = monotonic_ns();
    if (quote.side == pm::v7::Side::Buy) {
        quotes.bid_active = 1;
        quotes.bid_tick = quote.price_tick;
    } else {
        quotes.ask_active = 1;
        quotes.ask_tick = quote.price_tick;
    }

    // Make all placements economically negative immediately after the quote
    // becomes LIVE.  The exploration minimum-rest contract must win over this
    // non-safety re-evaluation and preserve queue exposure without an intent.
    auto negative = model;
    negative.markout_coefficients.fill(1.0);
    update.state_version += 1;
    update.socket_receive_monotonic_ns = monotonic_ns();
    const auto held = hot.on_market_update(update, inventory, quotes, risk, negative);
    assert(held.reason == pm::v7::maker::DecisionReason::ExplorationHold);
    assert(held.intent_count == 0);
    assert(held.exploration_max_rest_ns == model.exploration_max_rest_ns);
}

void test_exploration_minimum_rest_survives_transient_exploit_promotion() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto exploratory = profitable_model();
    exploratory.base_ev_se_per_share = 0.025;
    exploratory.robust_ev_z = 100.0;
    exploratory.min_robust_ev_per_share = 0.00001;
    exploratory.min_quote_lifetime_ns = 0;
    configure_exploration(exploratory);
    exploratory.exploration_confidence_z = 0.0;
    pm::v7::maker::InventorySnapshot inventory;
    inventory.yes_shares = 10.0;
    inventory.no_shares = 10.0;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    const auto opened = hot.on_market_update(update, inventory, quotes, risk, exploratory);
    assert(opened.reason == pm::v7::maker::DecisionReason::ExplorationQuote);
    assert(opened.intent_count == 1);
    const auto& quote = opened.intents[0];

    // A normal exploit candidate can become robustly positive while the quote
    // is still crossing the asynchronous decision -> OMS acknowledgement gap.
    // It must not erase the pending order's exploration lifetime contract or
    // enqueue a competing placement.
    auto exploit = profitable_model();
    exploit.min_quote_lifetime_ns = 0;
    configure_exploration(exploit);
    exploit.exploration_confidence_z = 0.0;
    update.state_version += 1;
    update.socket_receive_monotonic_ns = monotonic_ns();
    const auto promoted = hot.on_market_update(update, inventory, quotes, risk, exploit);
    assert(promoted.reason == pm::v7::maker::DecisionReason::ExplorationHold);
    assert(promoted.intent_count == 0);

    // Once the OMS reports LIVE, the same transient exploit regime must still
    // retain lifetime ownership on the original exploratory order.
    quotes.last_quote_monotonic_ns = monotonic_ns();
    if (quote.side == pm::v7::Side::Buy) {
        quotes.bid_active = 1;
        quotes.bid_tick = quote.price_tick;
    } else {
        quotes.ask_active = 1;
        quotes.ask_tick = quote.price_tick;
    }
    update.state_version += 1;
    update.socket_receive_monotonic_ns = monotonic_ns();
    const auto live_promoted = hot.on_market_update(update, inventory, quotes, risk, exploit);
    assert(live_promoted.reason == pm::v7::maker::DecisionReason::ExplorationHold);
    assert(live_promoted.intent_count == 0);

    // Returning below the exploit threshold before minimum_rest_ms must still
    // preserve the same quote. This is the exact live oscillation regression.
    auto negative = exploratory;
    negative.markout_coefficients.fill(1.0);
    update.state_version += 1;
    update.socket_receive_monotonic_ns = monotonic_ns();
    const auto held = hot.on_market_update(update, inventory, quotes, risk, negative);
    assert(held.reason == pm::v7::maker::DecisionReason::ExplorationHold);
    assert(held.intent_count == 0);
}

void test_negative_exploration_adjusted_ev_has_no_execution_authority() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    auto model = profitable_model();
    model.robust_ev_z = 1.64;
    model.markout_coefficients.fill(1.0);
    configure_exploration(model);
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 20.0;
    risk.exploration_max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 200.0;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::NoEconomicQuote);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Withdraw);
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

void test_new_risk_freeze_withdraws_without_irreversible_kill() {
    pm::v7::maker::MakerHotPath hot;
    auto update = normal_update();
    const auto model = profitable_model();
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.new_risk_frozen = 1;

    const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
    assert(decision.reason == pm::v7::maker::DecisionReason::NewRiskFrozen);
    assert(decision.intent_count == 1);
    assert(decision.intents[0].type == pm::v7::IntentType::Withdraw);
    assert(decision.intents[0].urgency == pm::v7::Urgency::Critical);
}

} // namespace

int main() {
    test_strategy_intent_is_pod();
    test_execution_cell_index_is_action_outcome_side_specific();
    test_bounded_spsc_and_cancel_priority();
    test_atomic_model_snapshot();
    test_profitable_two_sided_post_only_quote();
    test_authoritative_zero_flow_blocks_passive_quote();
    test_side_specific_opposite_flow_authorizes_only_reachable_side();
    test_trade_rate_uses_elapsed_time_not_book_message_count();
    test_specific_improve_cell_changes_candidate_ranking();
    test_settlement_fair_uses_lower_for_bid_and_upper_for_ask();
    test_outcome_cell_orientation_is_respected();
    test_sell_cell_controls_one_sided_inventory_reduction();
    test_inventory_forces_reducing_side();
    test_partial_inventory_sizes_reducing_quote_to_available_residual();
    test_transient_no_economic_quote_cannot_cancel_before_minimum_lifetime();
    test_positive_exploration_ev_breaks_robust_cold_start();
    test_point_ev_exploration_prefers_highest_economic_placement();
    test_exploration_collects_positive_buy_and_inventory_backed_sell_evidence();
    test_exploration_never_emits_uncovered_sell();
    test_persistent_lifetime_arm_is_explicit_and_bounded();
    test_exploration_minimum_rest_survives_transient_negative_ev();
    test_exploration_minimum_rest_survives_transient_exploit_promotion();
    test_negative_exploration_adjusted_ev_has_no_execution_authority();
    test_global_kill_preempts_quote();
    test_new_risk_freeze_withdraws_without_irreversible_kill();
    return 0;
}
