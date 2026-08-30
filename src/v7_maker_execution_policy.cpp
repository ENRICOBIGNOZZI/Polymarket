#include "pm/v7_maker_execution_policy.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7::maker {
namespace {

constexpr std::int64_t kPriceScale = 10'000;
constexpr long double kMicrodollarsPerDollar = 1'000'000.0L;

[[nodiscard]] bool terminal_kind(PaperMakerEventKind kind) noexcept {
    return kind == PaperMakerEventKind::Cancelled
        || kind == PaperMakerEventKind::Rejected;
}

} // namespace

bool MakerPaperExecutionPolicy::register_market(
    std::uint64_t market_handle,
    std::uint64_t yes_instrument_handle,
    std::uint64_t no_instrument_handle,
    std::int32_t tick_size_e4,
    PaperMakerPolicy policy) noexcept {

    if (market_handle == 0 || market_handle >= markets_.size()
        || yes_instrument_handle == 0 || no_instrument_handle == 0
        || yes_instrument_handle == no_instrument_handle
        || tick_size_e4 <= 0 || tick_size_e4 > kPriceScale
        || kPriceScale % tick_size_e4 != 0 || !policy.valid()) {
        return false;
    }
    auto& slot = markets_[static_cast<std::size_t>(market_handle)];
    if (slot.engine.has_value()) {
        return slot.yes_instrument_handle == yes_instrument_handle
            && slot.no_instrument_handle == no_instrument_handle
            && slot.tick_size_e4 == tick_size_e4;
    }
    slot.yes_instrument_handle = yes_instrument_handle;
    slot.no_instrument_handle = no_instrument_handle;
    slot.tick_size_e4 = tick_size_e4;
    slot.engine.emplace(market_handle, yes_instrument_handle, no_instrument_handle, policy);
    return true;
}

MakerPaperExecutionPolicy::MarketSlot* MakerPaperExecutionPolicy::market(
    std::uint64_t market_handle) noexcept {
    if (market_handle == 0 || market_handle >= markets_.size()) return nullptr;
    auto& slot = markets_[static_cast<std::size_t>(market_handle)];
    return slot.engine.has_value() ? &slot : nullptr;
}

const MakerPaperExecutionPolicy::MarketSlot* MakerPaperExecutionPolicy::market(
    std::uint64_t market_handle) const noexcept {
    if (market_handle == 0 || market_handle >= markets_.size()) return nullptr;
    const auto& slot = markets_[static_cast<std::size_t>(market_handle)];
    return slot.engine.has_value() ? &slot : nullptr;
}

std::size_t MakerPaperExecutionPolicy::hash(std::uint64_t intent_id) const noexcept {
    static_assert((kMaxMakerBuyReservations & (kMaxMakerBuyReservations - 1U)) == 0,
                  "buy reservation capacity must be a power of two");
    std::uint64_t x = intent_id + 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27U)) * 0x94d049bb133111ebULL;
    x ^= x >> 31U;
    return static_cast<std::size_t>(x) & (kMaxMakerBuyReservations - 1U);
}

MakerPaperExecutionPolicy::BuyReservationTrack* MakerPaperExecutionPolicy::track(
    std::uint64_t intent_id) noexcept {
    if (intent_id == 0) return nullptr;
    const std::size_t start = hash(intent_id);
    for (std::size_t step = 0; step < kMaxMakerBuyReservations; ++step) {
        auto& item = buy_tracks_[(start + step) & (kMaxMakerBuyReservations - 1U)];
        if (item.state == TrackState::Empty) return nullptr;
        if (item.state == TrackState::Active && item.intent_id == intent_id) return &item;
    }
    return nullptr;
}

const MakerPaperExecutionPolicy::BuyReservationTrack* MakerPaperExecutionPolicy::track(
    std::uint64_t intent_id) const noexcept {
    if (intent_id == 0) return nullptr;
    const std::size_t start = hash(intent_id);
    for (std::size_t step = 0; step < kMaxMakerBuyReservations; ++step) {
        const auto& item = buy_tracks_[(start + step) & (kMaxMakerBuyReservations - 1U)];
        if (item.state == TrackState::Empty) return nullptr;
        if (item.state == TrackState::Active && item.intent_id == intent_id) return &item;
    }
    return nullptr;
}

MakerPaperExecutionPolicy::BuyReservationTrack* MakerPaperExecutionPolicy::insert_track(
    std::uint64_t intent_id) noexcept {
    if (intent_id == 0) return nullptr;
    const std::size_t start = hash(intent_id);
    BuyReservationTrack* tombstone = nullptr;
    for (std::size_t step = 0; step < kMaxMakerBuyReservations; ++step) {
        auto& item = buy_tracks_[(start + step) & (kMaxMakerBuyReservations - 1U)];
        if (item.state == TrackState::Active && item.intent_id == intent_id) return &item;
        if (item.state == TrackState::Tombstone && tombstone == nullptr) tombstone = &item;
        if (item.state == TrackState::Empty) return tombstone != nullptr ? tombstone : &item;
    }
    return tombstone;
}

void MakerPaperExecutionPolicy::erase_track(BuyReservationTrack& item) noexcept {
    item = BuyReservationTrack{};
    item.state = TrackState::Tombstone;
}

bool MakerPaperExecutionPolicy::price_e4(
    const StrategyIntent& intent,
    std::int32_t tick_size_e4,
    std::int64_t& out) noexcept {
    out = 0;
    if (intent.price_tick <= 0 || tick_size_e4 <= 0 || tick_size_e4 >= kPriceScale) return false;
    const std::int64_t tick = static_cast<std::int64_t>(tick_size_e4);
    if (intent.price_tick > (kPriceScale - 1) / tick) return false;
    out = intent.price_tick * tick;
    return out > 0 && out < kPriceScale;
}

bool MakerPaperExecutionPolicy::cumulative_notional_microdollars(
    std::int64_t cumulative_fill_microunits,
    std::int64_t price_e4_value,
    std::int64_t& out) noexcept {
    out = 0;
    if (cumulative_fill_microunits <= 0 || price_e4_value <= 0
        || price_e4_value >= kPriceScale) {
        return false;
    }
    const long double raw = static_cast<long double>(cumulative_fill_microunits)
                          * static_cast<long double>(price_e4_value)
                          / static_cast<long double>(kPriceScale);
    if (!std::isfinite(raw) || raw <= 0.0L
        || raw > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        return false;
    }
    out = static_cast<std::int64_t>(std::ceil(raw));
    return out > 0;
}

bool MakerPaperExecutionPolicy::sync_inventory_capital(
    std::uint64_t market_handle,
    SleeveCapitalAccount& capital) noexcept {
    const auto* slot = market(market_handle);
    if (slot == nullptr) return false;
    const auto& inv = slot->engine->inventory();
    const long double cost = static_cast<long double>(std::max(0.0, inv.yes_cost))
                           + static_cast<long double>(std::max(0.0, inv.no_cost));
    const long double raw = cost * kMicrodollarsPerDollar;
    if (!std::isfinite(raw) || raw < 0.0L
        || raw > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        return false;
    }
    const std::int64_t target = static_cast<std::int64_t>(std::ceil(raw));
    const std::int64_t current = capital.inventory_committed_for_market(market_handle);
    if (target > current) return capital.commit_inventory(market_handle, target - current);
    if (target < current) return capital.release_inventory(market_handle, current - target);
    return true;
}

bool MakerPaperExecutionPolicy::process_paper_events(
    std::uint64_t market_handle,
    const PaperMakerResult& paper,
    SleeveCapitalAccount& capital) noexcept {
    bool ok = !paper.invariant_violation;
    for (std::size_t i = 0; i < paper.event_count; ++i) {
        const auto& event = paper.events[i];
        if (event.kind == PaperMakerEventKind::Fill
            && event.side == Side::Buy
            && event.operational_fill_microunits > 0) {
            auto* item = track(event.intent_id);
            if (item == nullptr || item->market_handle != market_handle
                || item->price_e4 <= 0 || item->original_microunits <= 0
                || event.operational_fill_microunits
                    > item->original_microunits - item->cumulative_fill_microunits) {
                ok = false;
                continue;
            }
            item->cumulative_fill_microunits += event.operational_fill_microunits;
            std::int64_t target_settled = 0;
            if (!cumulative_notional_microdollars(
                    item->cumulative_fill_microunits, item->price_e4, target_settled)
                || target_settled < item->settled_microdollars) {
                ok = false;
                continue;
            }
            const std::int64_t delta = target_settled - item->settled_microdollars;
            if (delta > 0 && !capital.settle_partial_fill(event.intent_id, delta)) {
                ok = false;
                continue;
            }
            item->settled_microdollars = target_settled;
            if (event.order_state == OrderState::Filled) {
                if (item->cumulative_fill_microunits != item->original_microunits) ok = false;
                erase_track(*item);
            }
            continue;
        }

        if (terminal_kind(event.kind) && event.side == Side::Buy && event.intent_id != 0) {
            auto* item = track(event.intent_id);
            if (item != nullptr) {
                // Cancel/reject makes the residual non-fillable. Filled cost was
                // already transferred on each partial fill; only the residual
                // reservation is released here.
                if (!capital.release_order(event.intent_id)) ok = false;
                erase_track(*item);
            }
        }
    }
    if (!sync_inventory_capital(market_handle, capital)) ok = false;
    return ok;
}

MakerExecutionResult MakerPaperExecutionPolicy::process(
    const ExecutionPlan& plan,
    SleeveCapitalAccount& capital) noexcept {
    MakerExecutionResult out;
    auto* slot = market(plan.intent.market_handle);
    if (slot == nullptr) {
        out.reason = MakerExecutionReason::UnknownMarket;
        return out;
    }

    const bool control = is_critical_intent(plan.intent.type)
        || plan.intent.urgency == Urgency::Critical;
    if ((!control && plan.policy != ExecutionPolicyId::PassiveMaker)
        || (control && plan.policy != ExecutionPolicyId::PassiveMaker
                    && plan.policy != ExecutionPolicyId::Emergency)) {
        out.reason = MakerExecutionReason::UnsupportedPolicy;
        return out;
    }
    if (plan.intent.type == IntentType::Quote) {
        if (!plan.public_queue_observable) {
            out.reason = MakerExecutionReason::QueueUnobservable;
            return out;
        }
        if (plan.tick_size_e4 != slot->tick_size_e4) {
            out.reason = MakerExecutionReason::AdmissionDenied;
            return out;
        }
    }

    if (plan.intent.type == IntentType::Quote && plan.intent.side == Side::Buy) {
        if (const auto* existing = track(plan.intent.intent_id); existing != nullptr) {
            std::int64_t px = 0;
            if (existing->market_handle == plan.intent.market_handle
                && existing->original_microunits == plan.intent.quantity_microunits
                && price_e4(plan.intent, slot->tick_size_e4, px)
                && existing->price_e4 == px) {
                out.reason = MakerExecutionReason::DuplicateNoop;
                out.accepted = 1;
                return out;
            }
            out.reason = MakerExecutionReason::CapitalInvariant;
            out.capital_invariant_violation = 1;
            return out;
        }
    }

    const auto admission = ExecutionAdmission::admit(
        plan.intent,
        plan.intent.type == IntentType::Quote ? slot->tick_size_e4 : plan.tick_size_e4,
        capital);
    if (!admission.accepted) {
        out.reason = MakerExecutionReason::AdmissionDenied;
        return out;
    }

    BuyReservationTrack* newly_tracked = nullptr;
    if (plan.intent.type == IntentType::Quote && plan.intent.side == Side::Buy) {
        std::int64_t px = 0;
        newly_tracked = insert_track(plan.intent.intent_id);
        if (newly_tracked == nullptr || newly_tracked->state == TrackState::Active
            || !price_e4(plan.intent, slot->tick_size_e4, px)) {
            if (admission.capital_reserved) (void)capital.release_order(plan.intent.intent_id);
            out.reason = MakerExecutionReason::CapitalInvariant;
            out.capital_invariant_violation = 1;
            return out;
        }
        *newly_tracked = BuyReservationTrack{};
        newly_tracked->intent_id = plan.intent.intent_id;
        newly_tracked->market_handle = plan.intent.market_handle;
        newly_tracked->price_e4 = px;
        newly_tracked->original_microunits = plan.intent.quantity_microunits;
        newly_tracked->state = TrackState::Active;
    }

    out.paper = slot->engine->apply_intent(
        plan.intent,
        plan.intent.type == IntentType::Quote ? plan.public_queue_ahead_microunits : 0,
        plan.intent.type == IntentType::Quote ? slot->tick_size_e4 : plan.tick_size_e4);

    if (out.paper.rejected || out.paper.invariant_violation) {
        if (newly_tracked != nullptr) {
            if (admission.capital_reserved && !capital.release_order(plan.intent.intent_id)) {
                out.capital_invariant_violation = 1;
            }
            erase_track(*newly_tracked);
        }
        out.reason = out.capital_invariant_violation
            ? MakerExecutionReason::CapitalInvariant : MakerExecutionReason::PaperRejected;
        return out;
    }

    if (!process_paper_events(plan.intent.market_handle, out.paper, capital)) {
        out.reason = MakerExecutionReason::CapitalInvariant;
        out.capital_invariant_violation = 1;
        return out;
    }
    out.reason = MakerExecutionReason::Applied;
    out.accepted = 1;
    return out;
}

MakerExecutionResult MakerPaperExecutionPolicy::on_public_trade(
    std::uint64_t market_handle,
    const PublicTradePrint& trade,
    SleeveCapitalAccount& capital) noexcept {
    MakerExecutionResult out;
    auto* slot = market(market_handle);
    if (slot == nullptr) {
        out.reason = MakerExecutionReason::UnknownMarket;
        return out;
    }
    out.paper = slot->engine->on_public_trade(trade);
    if (!process_paper_events(market_handle, out.paper, capital)) {
        out.reason = MakerExecutionReason::CapitalInvariant;
        out.capital_invariant_violation = 1;
        return out;
    }
    out.reason = MakerExecutionReason::Applied;
    out.accepted = static_cast<std::uint8_t>(!out.paper.rejected || out.paper.duplicate_trade);
    return out;
}

MakerExecutionResult MakerPaperExecutionPolicy::advance_time(
    std::uint64_t market_handle,
    std::int64_t monotonic_ns,
    SleeveCapitalAccount& capital) noexcept {
    MakerExecutionResult out;
    auto* slot = market(market_handle);
    if (slot == nullptr) {
        out.reason = MakerExecutionReason::UnknownMarket;
        return out;
    }
    out.paper = slot->engine->advance_time(monotonic_ns);
    if (!process_paper_events(market_handle, out.paper, capital)) {
        out.reason = MakerExecutionReason::CapitalInvariant;
        out.capital_invariant_violation = 1;
        return out;
    }
    out.reason = MakerExecutionReason::Applied;
    out.accepted = 1;
    return out;
}

MakerExecutionResult MakerPaperExecutionPolicy::cancel_all(
    std::uint64_t market_handle,
    std::int64_t monotonic_ns,
    SleeveCapitalAccount& capital) noexcept {
    MakerExecutionResult out;
    auto* slot = market(market_handle);
    if (slot == nullptr) {
        out.reason = MakerExecutionReason::UnknownMarket;
        return out;
    }
    out.paper = slot->engine->cancel_all(monotonic_ns);
    if (out.paper.rejected || out.paper.invariant_violation) {
        out.reason = MakerExecutionReason::PaperRejected;
        return out;
    }
    if (!process_paper_events(market_handle, out.paper, capital)) {
        out.reason = MakerExecutionReason::CapitalInvariant;
        out.capital_invariant_violation = 1;
        return out;
    }
    out.reason = MakerExecutionReason::Applied;
    out.accepted = 1;
    return out;
}

MakerExecutionResult MakerPaperExecutionPolicy::liquidate_directional_inventory(
    std::uint64_t market_handle,
    std::int64_t yes_best_bid_tick,
    std::int64_t yes_bid_depth_microunits,
    std::int64_t no_best_bid_tick,
    std::int64_t no_bid_depth_microunits,
    std::int64_t timestamp_ns,
    SleeveCapitalAccount& capital) noexcept {
    MakerExecutionResult out;
    auto* slot = market(market_handle);
    if (slot == nullptr) {
        out.reason = MakerExecutionReason::UnknownMarket;
        return out;
    }
    out.paper = slot->engine->liquidate_directional_inventory(
        yes_best_bid_tick, yes_bid_depth_microunits,
        no_best_bid_tick, no_bid_depth_microunits,
        slot->tick_size_e4, timestamp_ns);
    if (out.paper.rejected || out.paper.invariant_violation) {
        out.reason = MakerExecutionReason::PaperRejected;
        return out;
    }
    if (!sync_inventory_capital(market_handle, capital)) {
        out.reason = MakerExecutionReason::CapitalInvariant;
        out.capital_invariant_violation = 1;
        return out;
    }
    out.reason = MakerExecutionReason::Applied;
    out.accepted = 1;
    return out;
}

MakerExecutionResult MakerPaperExecutionPolicy::split_complete_sets(
    std::uint64_t market_handle,
    std::int64_t microunits,
    std::int64_t timestamp_ns,
    double yes_reference_price,
    SleeveCapitalAccount& capital) noexcept {
    MakerExecutionResult out;
    auto* slot = market(market_handle);
    if (slot == nullptr) {
        out.reason = MakerExecutionReason::UnknownMarket;
        return out;
    }
    out.paper = slot->engine->split_complete_sets(
        capital, microunits, timestamp_ns, yes_reference_price);
    if (out.paper.rejected || out.paper.invariant_violation) {
        out.reason = MakerExecutionReason::PaperRejected;
        return out;
    }
    if (!sync_inventory_capital(market_handle, capital)) {
        out.reason = MakerExecutionReason::CapitalInvariant;
        out.capital_invariant_violation = 1;
        return out;
    }
    out.reason = MakerExecutionReason::Applied;
    out.accepted = 1;
    return out;
}

bool MakerPaperExecutionPolicy::set_complete_set_reserve_floor(
    std::uint64_t market_handle,
    std::int64_t microunits) noexcept {
    auto* slot = market(market_handle);
    return slot != nullptr
        && slot->engine->set_complete_set_reserve_floor(microunits);
}

QuoteSnapshot MakerPaperExecutionPolicy::quote_snapshot(
    std::uint64_t market_handle,
    std::uint64_t instrument_handle) const noexcept {
    const auto* slot = market(market_handle);
    return slot == nullptr ? QuoteSnapshot{} : slot->engine->quote_snapshot(instrument_handle);
}

InventorySnapshot MakerPaperExecutionPolicy::inventory_snapshot(
    std::uint64_t market_handle) const noexcept {
    const auto* slot = market(market_handle);
    return slot == nullptr ? InventorySnapshot{} : slot->engine->inventory_snapshot();
}

const PaperMakerInventory* MakerPaperExecutionPolicy::inventory(
    std::uint64_t market_handle) const noexcept {
    const auto* slot = market(market_handle);
    return slot == nullptr ? nullptr : &slot->engine->inventory();
}

} // namespace pm::v7::maker
