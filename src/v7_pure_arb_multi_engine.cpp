#include "pm/v7_pure_arb_multi_engine.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>

namespace pm::v7::pure_arb {
namespace {
[[nodiscard]] std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
[[nodiscard]] std::size_t map_hash(std::uint64_t value) noexcept {
    value ^= value >> 33U;
    value *= 0xff51afd7ed558ccdULL;
    value ^= value >> 33U;
    return static_cast<std::size_t>(value)
        & (kPureArbInstrumentMapCapacity - 1U);
}
static_assert((kPureArbInstrumentMapCapacity
               & (kPureArbInstrumentMapCapacity - 1U)) == 0);
} // namespace

MultiMarketEngine::MultiMarketEngine(
    std::span<const MultiMarketConfig> configs,
    NativeSettlementAuthority& authority,
    NativeLatencyTape* latency_tape) noexcept
    : authority_(authority), latency_tape_(latency_tape) {
    if (configs.empty() || configs.size() > markets_.size()) return;
    for (std::size_t i = 0; i < configs.size(); ++i) {
        const auto& c = configs[i];
        const bool ok =
            c.market_handle != 0 && c.event_handle != 0
            && c.yes_instrument_handle != 0 && c.no_instrument_handle != 0
            && c.yes_instrument_handle != c.no_instrument_handle
            && c.market_start_wall_ms > 0
            && c.market_end_wall_ms > c.market_start_wall_ms
            && c.maximum_leg_skew_ns > 0
            && c.minimum_order_microunits > 0
            && std::isfinite(c.fee_rate) && c.fee_rate >= 0.0 && c.fee_rate <= 1.0
            && std::isfinite(c.fee_exponent) && c.fee_exponent >= 0.0
            && std::isfinite(c.reserve_per_share) && c.reserve_per_share >= 0.0
            && c.fee_verified != 0;
        if (!ok || !install(c.yes_instrument_handle,
                            static_cast<std::uint16_t>(i), true)
                || !install(c.no_instrument_handle,
                            static_cast<std::uint16_t>(i), false)) {
            return;
        }
        markets_[i].config = c;
    }
    count_ = configs.size();
    valid_ = true;
}

bool MultiMarketEngine::install(
    std::uint64_t instrument, std::uint16_t context, bool yes) noexcept {
    const auto start = map_hash(instrument);
    for (std::size_t probe = 0; probe < instrument_map_.size(); ++probe) {
        auto& slot = instrument_map_[(start + probe)
            & (instrument_map_.size() - 1U)];
        if (slot.occupied != 0) {
            if (slot.instrument_handle == instrument) return false;
            continue;
        }
        slot.instrument_handle = instrument;
        slot.context_index = context;
        slot.yes_leg = yes ? 1 : 0;
        slot.occupied = 1;
        return true;
    }
    return false;
}

MultiMarketEngine::InstrumentMap* MultiMarketEngine::lookup(
    std::uint64_t instrument) noexcept {
    if (instrument == 0) return nullptr;
    const auto start = map_hash(instrument);
    for (std::size_t probe = 0; probe < instrument_map_.size(); ++probe) {
        auto& slot = instrument_map_[(start + probe)
            & (instrument_map_.size() - 1U)];
        if (slot.occupied == 0) return nullptr;
        if (slot.instrument_handle == instrument) return &slot;
    }
    return nullptr;
}

const MultiMarketEngine::InstrumentMap* MultiMarketEngine::lookup(
    std::uint64_t instrument) const noexcept {
    if (instrument == 0) return nullptr;
    const auto start = map_hash(instrument);
    for (std::size_t probe = 0; probe < instrument_map_.size(); ++probe) {
        const auto& slot = instrument_map_[(start + probe)
            & (instrument_map_.size() - 1U)];
        if (slot.occupied == 0) return nullptr;
        if (slot.instrument_handle == instrument) return &slot;
    }
    return nullptr;
}

std::uint64_t MultiMarketEngine::next_intent_id() noexcept {
    ++next_intent_id_;
    if (next_intent_id_ == 0) ++next_intent_id_;
    return next_intent_id_;
}

void MultiMarketEngine::publish_prefix(
    std::uint64_t client_order_id,
    const PureArbExecutionPlan& plan,
    std::int64_t risk_admitted_ns) noexcept {
    if (latency_tape_ == nullptr || client_order_id == 0) return;
    const auto publish = [&](NativeLatencyStage stage,
                             std::int64_t timestamp) noexcept {
        if (timestamp <= 0) return;
        NativeLatencyEvent event{};
        event.trace_id = client_order_id;
        event.client_order_id = client_order_id;
        event.market_handle = plan.market_handle;
        event.timestamp_ns = timestamp;
        event.stage = stage;
        (void)latency_tape_->publish(event);
    };
    publish(NativeLatencyStage::FrameReceive,
            plan.trigger_receive_monotonic_ns);
    publish(NativeLatencyStage::DecodeDone,
            plan.decode_complete_monotonic_ns);
    publish(NativeLatencyStage::ArbDecision,
            plan.decision_monotonic_ns);
    publish(NativeLatencyStage::RiskAdmitted,
            risk_admitted_ns);
}

MultiMarketDecision MultiMarketEngine::on_market_event(
    const MarketWsEvent& event,
    std::uint64_t connection_epoch,
    std::int64_t now_wall_ms) noexcept {
    MultiMarketDecision out{};
    if (!valid_ || connection_epoch == 0 || now_wall_ms <= 0) return out;
    auto* binding = lookup(event.instrument_handle);
    if (binding == nullptr || binding->context_index >= count_) return out;
    auto& state = markets_[binding->context_index];
    out.context_index = binding->context_index;

    if (event.kind == MarketWsEventKind::LineageInvalidated) {
        if (binding->yes_leg != 0) {
            state.yes = {};
            state.yes_epoch = 0;
        } else {
            state.no = {};
            state.no_epoch = 0;
        }
        return out;
    }
    if (binding->yes_leg != 0) {
        state.yes = event.book;
        state.yes_epoch = connection_epoch;
    } else {
        state.no = event.book;
        state.no_epoch = connection_epoch;
    }

    PairInput input{};
    input.market_handle = state.config.market_handle;
    input.event_handle = state.config.event_handle;
    input.yes_instrument_handle = state.config.yes_instrument_handle;
    input.no_instrument_handle = state.config.no_instrument_handle;
    input.yes_epoch = state.yes_epoch;
    input.no_epoch = state.no_epoch;
    input.market_start_wall_ms = state.config.market_start_wall_ms;
    input.market_end_wall_ms = state.config.market_end_wall_ms;
    input.now_wall_ms = now_wall_ms;
    input.trigger_receive_monotonic_ns = event.receive_monotonic_ns;
    input.decode_complete_monotonic_ns =
        event.decode_complete_monotonic_ns;
    input.maximum_leg_skew_ns = state.config.maximum_leg_skew_ns;
    input.minimum_order_microunits =
        state.config.minimum_order_microunits;
    const auto yes_inventory = authority_.inventory_snapshot(
        state.config.yes_instrument_handle);
    const auto no_inventory = authority_.inventory_snapshot(
        state.config.no_instrument_handle);
    input.sell_available_microunits = std::min(
        yes_inventory.available_microunits,
        no_inventory.available_microunits);
    input.fee_rate = state.config.fee_rate;
    input.fee_exponent = state.config.fee_exponent;
    input.reserve_per_share = state.config.reserve_per_share;
    input.fee_verified = state.config.fee_verified;
    input.yes = state.yes;
    input.no = state.no;

    out.plan = evaluate_pair(input);
    out.evaluated = 1;
    out.plan.decision_monotonic_ns = monotonic_ns();
    if (out.plan.accepted == 0) return out;

    ExecutionPlan yes_plan{}, no_plan{};
    if (!make_execution_plan(
            out.plan, true, next_intent_id(), yes_plan)
        || !make_execution_plan(
            out.plan, false, next_intent_id(), no_plan)) {
        out.plan.accepted = 0;
        out.plan.reason = DecisionReason::InvalidInput;
        return out;
    }
    const auto risk_start_ns = monotonic_ns();
    out.admission = authority_.submit_pair(
        yes_plan, no_plan,
        state.config.minimum_order_microunits,
        risk_start_ns);
    if (out.admission.accepted == 0) return out;
    out.admitted = 1;
    publish_prefix(
        out.admission.yes.tx.command.client_order_id,
        out.plan, out.admission.risk_admitted_monotonic_ns);
    publish_prefix(
        out.admission.no.tx.command.client_order_id,
        out.plan, out.admission.risk_admitted_monotonic_ns);
    return out;
}

void MultiMarketEngine::invalidate_all() noexcept {
    for (std::size_t i = 0; i < count_; ++i) {
        markets_[i].yes = {};
        markets_[i].no = {};
        markets_[i].yes_epoch = 0;
        markets_[i].no_epoch = 0;
    }
}

} // namespace pm::v7::pure_arb
