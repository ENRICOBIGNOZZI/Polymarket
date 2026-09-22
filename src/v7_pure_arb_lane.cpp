#include "pm/v7_pure_arb_lane.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7::pure_arb {
namespace {

[[nodiscard]] bool executable_book(const BookHotSnapshot& book) noexcept {
    return book.valid != 0
        && book.lineage_continuous != 0
        && book.best_bid_e4 > 0
        && book.best_ask_e4 > book.best_bid_e4
        && book.best_ask_e4 < 10'000
        && book.best_bid_microunits > 0
        && book.best_ask_microunits > 0
        && book.bid_level_count > 0
        && book.ask_level_count > 0;
}

[[nodiscard]] std::int64_t absolute_difference(
    std::int64_t lhs, std::int64_t rhs) noexcept {
    return lhs >= rhs ? lhs - rhs : rhs - lhs;
}

} // namespace

bool PureArbLane::valid() const noexcept {
    return context_.market_handle != 0
        && context_.event_handle != 0
        && context_.yes_instrument_handle != 0
        && context_.no_instrument_handle != 0
        && context_.yes_instrument_handle != context_.no_instrument_handle
        && context_.fee_verified != 0
        && std::isfinite(context_.fee_rate)
        && context_.fee_rate >= 0.0
        && context_.fee_rate <= 1.0
        && std::isfinite(context_.fee_exponent)
        && context_.fee_exponent >= 0.0
        && std::isfinite(context_.reserve_per_share)
        && context_.reserve_per_share >= 0.0
        && context_.minimum_order_microunits > 0
        && context_.maximum_leg_skew_ns >= 0
        && context_.sell_capacity_microunits >= 0;
}

Evaluation PureArbLane::evaluate(const PairInput& input) const noexcept {
    Evaluation out{};
    if (!valid()) {
        out.reason = RejectReason::InvalidContext;
        return out;
    }

    if (input.yes_connection_epoch == 0
        || input.yes_connection_epoch != input.no_connection_epoch) {
        out.reason = RejectReason::EpochMismatch;
        return out;
    }
    out.epoch_synced = 1;

    if (input.yes.receive_monotonic_ns <= 0
        || input.no.receive_monotonic_ns <= 0
        || absolute_difference(
               input.yes.receive_monotonic_ns,
               input.no.receive_monotonic_ns) > context_.maximum_leg_skew_ns) {
        out.reason = RejectReason::LegSkewExceeded;
        return out;
    }
    out.leg_skew_ready = 1;

    if (input.yes.valid == 0 || input.no.valid == 0
        || input.yes.lineage_continuous == 0
        || input.no.lineage_continuous == 0) {
        out.reason = RejectReason::LineageInvalid;
        return out;
    }
    out.lineage_ready = 1;

    if (!executable_book(input.yes) || !executable_book(input.no)) {
        out.reason = RejectReason::BookInvalid;
        return out;
    }
    out.book_valid = 1;

    const double yes_ask = price(input.yes.best_ask_e4);
    const double no_ask = price(input.no.best_ask_e4);
    const double yes_bid = price(input.yes.best_bid_e4);
    const double no_bid = price(input.no.best_bid_e4);
    const double buy_shares_l1 = static_cast<double>(std::min(
        input.yes.best_ask_microunits,
        input.no.best_ask_microunits)) / kMicrounitsPerShare;
    const double sell_shares_l1 = static_cast<double>(std::min(
        input.yes.best_bid_microunits,
        input.no.best_bid_microunits)) / kMicrounitsPerShare;

    if (!(buy_shares_l1 > 0.0) || !(sell_shares_l1 > 0.0)) {
        out.reason = RejectReason::BookInvalid;
        return out;
    }

    out.buy_fee_per_share =
        (fee_usdc(buy_shares_l1, yes_ask,
                  context_.fee_rate, context_.fee_exponent)
         + fee_usdc(buy_shares_l1, no_ask,
                    context_.fee_rate, context_.fee_exponent))
        / buy_shares_l1;
    out.sell_fee_per_share =
        (fee_usdc(sell_shares_l1, yes_bid,
                  context_.fee_rate, context_.fee_exponent)
         + fee_usdc(sell_shares_l1, no_bid,
                    context_.fee_rate, context_.fee_exponent))
        / sell_shares_l1;
    if (!std::isfinite(out.buy_fee_per_share)
        || !std::isfinite(out.sell_fee_per_share)) {
        out.reason = RejectReason::FeeInvalid;
        return out;
    }
    out.fee_finite = 1;

    out.buy_raw_edge_per_share = 1.0 - yes_ask - no_ask;
    out.sell_raw_edge_per_share = yes_bid + no_bid - 1.0;
    out.buy_edge_per_share =
        out.buy_raw_edge_per_share - out.buy_fee_per_share;
    out.sell_edge_per_share =
        out.sell_raw_edge_per_share - out.sell_fee_per_share;

    out.buy = sweep(
        input.yes, input.no,
        context_.fee_rate, context_.fee_exponent,
        context_.reserve_per_share, true);
    out.sell = sweep(
        input.yes, input.no,
        context_.fee_rate, context_.fee_exponent,
        context_.reserve_per_share, false,
        context_.sell_capacity_microunits);

    out.buy_minimum_met =
        out.buy.shares_microunits >= context_.minimum_order_microunits ? 1 : 0;
    out.sell_minimum_met =
        out.sell.shares_microunits >= context_.minimum_order_microunits ? 1 : 0;

    if (out.buy_minimum_met != 0 && out.sell_minimum_met != 0) {
        out.reason = RejectReason::AmbiguousDirection;
        return out;
    }
    if (out.buy_minimum_met == 0 && out.sell_minimum_met == 0) {
        out.reason = (out.buy.shares_microunits > 0
                      || out.sell.shares_microunits > 0)
            ? RejectReason::BelowVenueMinimum
            : RejectReason::NoPositiveEdge;
        return out;
    }

    const bool buy = out.buy_minimum_met != 0;
    const auto& economics = buy ? out.buy : out.sell;
    auto& plan = out.plan;
    plan.market_handle = context_.market_handle;
    plan.event_handle = context_.event_handle;
    plan.trigger_receive_monotonic_ns = std::max(
        input.yes.receive_monotonic_ns,
        input.no.receive_monotonic_ns);
    plan.decode_complete_monotonic_ns = input.decode_complete_monotonic_ns;
    plan.decision_monotonic_ns = input.decision_monotonic_ns;
    if (plan.decision_monotonic_ns > 0
        && plan.decision_monotonic_ns < plan.trigger_receive_monotonic_ns) {
        out.reason = RejectReason::InvalidContext;
        return out;
    }
    plan.direction = buy
        ? Direction::BuyCompleteSet
        : Direction::SellCompleteSet;
    plan.yes.instrument_handle = context_.yes_instrument_handle;
    plan.yes.state_version = input.yes.state_version;
    plan.yes.exchange_event_ns = input.yes.exchange_event_ns;
    plan.yes.quantity_microunits = economics.shares_microunits;
    plan.yes.limit_price_e4 = economics.yes_limit_price_e4;
    plan.yes.tick_size_e4 = input.yes.tick_size_e4;
    plan.yes.side = buy ? Side::Buy : Side::Sell;
    plan.no.instrument_handle = context_.no_instrument_handle;
    plan.no.state_version = input.no.state_version;
    plan.no.exchange_event_ns = input.no.exchange_event_ns;
    plan.no.quantity_microunits = economics.shares_microunits;
    plan.no.limit_price_e4 = economics.no_limit_price_e4;
    plan.no.tick_size_e4 = input.no.tick_size_e4;
    plan.no.side = buy ? Side::Buy : Side::Sell;
    plan.economics = economics;
    plan.valid = 1;
    out.reason = RejectReason::Accepted;
    return out;
}


bool make_execution_plan(
    const PureArbExecutionPlan& pair,
    bool yes_leg,
    std::uint64_t intent_id,
    ExecutionPlan& out) noexcept {
    out = {};
    if (pair.valid == 0 || intent_id == 0 || pair.market_handle == 0
        || pair.trigger_receive_monotonic_ns <= 0
        || pair.decision_monotonic_ns < pair.trigger_receive_monotonic_ns) {
        return false;
    }
    const auto& leg = yes_leg ? pair.yes : pair.no;
    if (leg.instrument_handle == 0 || leg.state_version == 0
        || leg.quantity_microunits <= 0 || leg.limit_price_e4 <= 0
        || leg.limit_price_e4 >= 10'000 || leg.tick_size_e4 <= 0
        || leg.limit_price_e4 % leg.tick_size_e4 != 0
        || (leg.side != Side::Buy && leg.side != Side::Sell)) {
        return false;
    }
    StrategyIntent intent{};
    intent.intent_id = intent_id;
    intent.market_handle = pair.market_handle;
    intent.event_handle = pair.event_handle;
    intent.instrument_handle = leg.instrument_handle;
    intent.state_version = leg.state_version;
    intent.causal_trigger_receive_monotonic_ns =
        pair.trigger_receive_monotonic_ns;
    intent.decode_complete_monotonic_ns =
        pair.decode_complete_monotonic_ns;
    intent.signal_ready_monotonic_ns = pair.decision_monotonic_ns;
    intent.decision_monotonic_ns = pair.decision_monotonic_ns;
    intent.exchange_event_ns = leg.exchange_event_ns;
    intent.price_tick = leg.limit_price_e4 / leg.tick_size_e4;
    intent.quantity_microunits = leg.quantity_microunits;
    intent.strategy_id = StrategyId::HardArbitrage;
    intent.type = IntentType::TargetPosition;
    intent.side = leg.side;
    intent.urgency = Urgency::Aggressive;
    intent.purpose = IntentPurpose::Alpha;
    intent.passive = 0;
    intent.post_only = 0;
    intent.expected_edge = pair.economics.conservative_edge_per_share();
    intent.expected_ev = pair.economics.conservative_locked_pnl;
    out.intent = intent;
    out.tick_size_e4 = leg.tick_size_e4;
    out.market_state_version = leg.state_version;
    out.policy = ExecutionPolicyId::PureArbFok;
    return true;
}

} // namespace pm::v7::pure_arb
