#include "pm/v7_redundant_bbo.hpp"

#include <algorithm>

namespace pm::v7::redundant_bbo {
namespace {
[[nodiscard]] constexpr std::uint8_t lane_bit(std::uint8_t lane) noexcept {
    return static_cast<std::uint8_t>(1U << lane);
}
[[nodiscard]] constexpr int popcount3(std::uint8_t value) noexcept {
    return ((value & 1U) != 0) + ((value & 2U) != 0) + ((value & 4U) != 0);
}
}

std::uint8_t Gate::matching_mask(const Slot& slot) noexcept {
    std::uint8_t best = 0;
    for (std::uint8_t i = 0; i < kLaneCount; ++i) {
        const auto bi = lane_bit(i);
        if ((slot.seen_mask & bi) == 0 || (slot.conflict_mask & bi) != 0) continue;
        std::uint8_t match = bi;
        for (std::uint8_t j = static_cast<std::uint8_t>(i + 1); j < kLaneCount; ++j) {
            const auto bj = lane_bit(j);
            if ((slot.seen_mask & bj) == 0 || (slot.conflict_mask & bj) != 0) continue;
            if (slot.bid_e4[i] == slot.bid_e4[j] && slot.ask_e4[i] == slot.ask_e4[j]) match |= bj;
        }
        if (popcount3(match) > popcount3(best)) best = match;
    }
    return best;
}

Decision Gate::start_epoch(Slot& slot, const Envelope& envelope) noexcept {
    if (slot.exchange_event_ns != 0 && slot.emitted == 0 && popcount3(matching_mask(slot)) < 2)
        ++metrics_.superseded_without_quorum;
    slot = {};
    slot.exchange_event_ns = envelope.update.exchange_event_ns;
    slot.first_receive_ns = envelope.update.receive_monotonic_ns;
    const auto lane = envelope.lane;
    const auto bit = lane_bit(lane);
    slot.bid_e4[lane] = envelope.update.best_bid_e4;
    slot.ask_e4[lane] = envelope.update.best_ask_e4;
    slot.receive_ns[lane] = envelope.update.receive_monotonic_ns;
    slot.seen_mask = bit;

    Decision out;
    out.update = envelope.update;
    out.observed_lanes_mask = bit;
    out.first_receive_monotonic_ns = slot.first_receive_ns;
    if (mode_ == Mode::FirstOf3Shadow) {
        slot.emitted = 1;
        out.outcome = Outcome::Actionable;
        out.agreeing_lanes_mask = bit;
        out.ready_monotonic_ns = envelope.update.receive_monotonic_ns;
        ++metrics_.actionable;
    }
    return out;
}

Decision Gate::decide(Slot& slot, const Envelope& envelope) noexcept {
    Decision out;
    out.update = envelope.update;
    out.first_receive_monotonic_ns = slot.first_receive_ns;
    const auto lane = envelope.lane;
    const auto bit = lane_bit(lane);

    if ((slot.seen_mask & bit) != 0) {
        if (slot.bid_e4[lane] == envelope.update.best_bid_e4
            && slot.ask_e4[lane] == envelope.update.best_ask_e4) {
            out.outcome = Outcome::Duplicate;
            out.observed_lanes_mask = slot.seen_mask;
            out.conflicting_lanes_mask = slot.conflict_mask;
            ++metrics_.duplicates;
            return out;
        }
        slot.conflict_mask |= bit;
        out.outcome = Outcome::Conflict;
        out.observed_lanes_mask = slot.seen_mask;
        out.conflicting_lanes_mask = slot.conflict_mask;
        ++metrics_.conflicts;
        if (slot.emitted != 0) ++metrics_.post_emit_conflicts;
        return out;
    }

    slot.seen_mask |= bit;
    slot.bid_e4[lane] = envelope.update.best_bid_e4;
    slot.ask_e4[lane] = envelope.update.best_ask_e4;
    slot.receive_ns[lane] = envelope.update.receive_monotonic_ns;
    out.observed_lanes_mask = slot.seen_mask;
    const auto match = matching_mask(slot);

    if (mode_ == Mode::FirstOf3Shadow) {
        const auto first_lane = static_cast<std::uint8_t>(
            slot.receive_ns[0] == slot.first_receive_ns ? 0
            : slot.receive_ns[1] == slot.first_receive_ns ? 1 : 2);
        const bool same_as_first = slot.bid_e4[lane] == slot.bid_e4[first_lane]
            && slot.ask_e4[lane] == slot.ask_e4[first_lane];
        if (!same_as_first) {
            slot.conflict_mask |= bit;
            out.outcome = Outcome::Conflict;
            out.conflicting_lanes_mask = slot.conflict_mask;
            ++metrics_.conflicts;
            ++metrics_.post_emit_conflicts;
        } else {
            out.outcome = Outcome::Duplicate;
            out.agreeing_lanes_mask = match;
            ++metrics_.duplicates;
        }
        return out;
    }

    if (slot.emitted == 0 && popcount3(match) >= 2) {
        slot.emitted = 1;
        std::uint8_t representative = 0;
        while ((match & lane_bit(representative)) == 0) ++representative;
        out.update.best_bid_e4 = slot.bid_e4[representative];
        out.update.best_ask_e4 = slot.ask_e4[representative];
        out.update.receive_monotonic_ns = envelope.update.receive_monotonic_ns;
        out.outcome = Outcome::Actionable;
        out.agreeing_lanes_mask = match;
        out.ready_monotonic_ns = envelope.update.receive_monotonic_ns;
        ++metrics_.actionable;
        return out;
    }

    if (popcount3(slot.seen_mask) == 3 && popcount3(match) < 2) {
        slot.conflict_mask = slot.seen_mask;
        out.outcome = Outcome::Conflict;
        out.conflicting_lanes_mask = slot.conflict_mask;
        ++metrics_.conflicts;
        return out;
    }
    return out;
}

Decision Gate::observe(const Envelope& envelope) noexcept {
    ++metrics_.observations;
    const auto& u = envelope.update;
    if (envelope.lane >= kLaneCount || u.valid == 0 || u.instrument_handle == 0
        || u.exchange_event_ns <= 0 || u.receive_monotonic_ns <= 0
        || u.best_bid_e4 <= 0 || u.best_ask_e4 <= u.best_bid_e4) {
        ++metrics_.invalid;
        Decision out; out.update = u; out.outcome = Outcome::Invalid; return out;
    }
    if (u.instrument_handle >= kInstrumentCapacity) {
        ++metrics_.out_of_range;
        Decision out; out.update = u; out.outcome = Outcome::OutOfRange; return out;
    }
    auto& slot = slots_[u.instrument_handle];
    if (slot.exchange_event_ns == 0 || u.exchange_event_ns > slot.exchange_event_ns)
        return start_epoch(slot, envelope);
    if (u.exchange_event_ns < slot.exchange_event_ns) {
        ++metrics_.stale;
        Decision out; out.update = u; out.outcome = Outcome::Stale;
        out.observed_lanes_mask = slot.seen_mask; return out;
    }
    return decide(slot, envelope);
}

void Gate::reset_instrument(std::uint64_t instrument_handle) noexcept {
    if (instrument_handle > 0 && instrument_handle < slots_.size()) slots_[instrument_handle] = {};
}
void Gate::reset_all() noexcept {
    slots_ = {};
    metrics_ = {};
}

} // namespace pm::v7::redundant_bbo
