#include "pm/v7_maker_paper.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7::maker {
namespace {

constexpr double kMicrounitsPerShare = 1'000'000.0;
constexpr double kPriceScaleE4 = 10'000.0;

[[nodiscard]] bool terminal(OrderState state) noexcept {
    return state == OrderState::Filled || state == OrderState::Cancelled
        || state == OrderState::Rejected || state == OrderState::Expired
        || state == OrderState::Lost;
}

[[nodiscard]] bool queue_active(OrderState state) noexcept {
    return state == OrderState::Live || state == OrderState::Partial
        || state == OrderState::CancelRequested || state == OrderState::CancelPending;
}

[[nodiscard]] double shares(std::int64_t microunits) noexcept {
    return static_cast<double>(std::max<std::int64_t>(0, microunits)) / kMicrounitsPerShare;
}

[[nodiscard]] double price(std::int64_t price_tick, std::int32_t tick_size_e4) noexcept {
    return static_cast<double>(price_tick) * static_cast<double>(tick_size_e4) / kPriceScaleE4;
}

[[nodiscard]] std::int64_t scaled_queue(std::int64_t visible, double multiplier) noexcept {
    if (visible <= 0) return 0;
    const long double raw = static_cast<long double>(visible) * static_cast<long double>(multiplier);
    if (raw >= static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        return std::numeric_limits<std::int64_t>::max();
    }
    return static_cast<std::int64_t>(std::ceil(raw));
}

[[nodiscard]] std::int64_t collateral_microdollars(
    double yes_cost, double no_cost) noexcept {
    const long double raw = static_cast<long double>(std::max(0.0, yes_cost)
        + std::max(0.0, no_cost)) * kMicrounitsPerShare;
    if (!std::isfinite(raw) || raw <= 0.0L) return 0;
    if (raw >= static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        return std::numeric_limits<std::int64_t>::max();
    }
    return static_cast<std::int64_t>(std::ceil(raw));
}

} // namespace

bool PaperMakerPolicy::valid() const noexcept {
    return assumed_submission_latency_ns >= 0 && cancel_latency_ns >= 0
        && std::isfinite(queue_expected_multiplier)
        && std::isfinite(queue_upper_multiplier)
        && std::isfinite(queue_confidence)
        && queue_expected_multiplier >= 1.0
        && queue_upper_multiplier >= queue_expected_multiplier
        && queue_confidence >= 0.0 && queue_confidence <= 1.0
        && operational_fill_is_pessimistic == 1;
}

MakerPaperMarketEngine::MakerPaperMarketEngine(
    std::uint64_t market_handle,
    std::uint64_t yes_instrument_handle,
    std::uint64_t no_instrument_handle,
    PaperMakerPolicy policy) noexcept
    : market_handle_(market_handle),
      yes_instrument_handle_(yes_instrument_handle),
      no_instrument_handle_(no_instrument_handle),
      policy_(policy) {}

bool MakerPaperMarketEngine::set_complete_set_reserve_floor(
    std::int64_t microunits) noexcept {
    if (microunits < 0) return false;
    inventory_.complete_set_reserve_floor_microunits = microunits;
    refresh_inventory_derived();
    return true;
}

bool MakerPaperMarketEngine::instrument_known(std::uint64_t instrument_handle) const noexcept {
    return instrument_handle != 0
        && (instrument_handle == yes_instrument_handle_ || instrument_handle == no_instrument_handle_);
}

bool MakerPaperMarketEngine::is_yes(std::uint64_t instrument_handle) const noexcept {
    return instrument_handle == yes_instrument_handle_;
}

bool MakerPaperMarketEngine::trade_seen(std::uint64_t trade_id) const noexcept {
    if (trade_id == 0) return false;
    return std::find(recent_trade_ids_.begin(), recent_trade_ids_.end(), trade_id)
        != recent_trade_ids_.end();
}

void MakerPaperMarketEngine::remember_trade(std::uint64_t trade_id) noexcept {
    recent_trade_ids_[recent_trade_cursor_] = trade_id;
    recent_trade_cursor_ = (recent_trade_cursor_ + 1) % recent_trade_ids_.size();
}

MakerPaperMarketEngine::Slot* MakerPaperMarketEngine::free_slot() noexcept {
    for (auto& slot : slots_) {
        if (!slot.occupied || terminal(slot.oms.record().state)) return &slot;
    }
    return nullptr;
}

MakerPaperMarketEngine::Slot* MakerPaperMarketEngine::active_slot(
    std::uint64_t instrument_handle, Side side) noexcept {
    for (auto& slot : slots_) {
        if (slot.occupied && slot.oms.record().instrument_handle == instrument_handle
            && slot.oms.record().side == side && queue_active(slot.oms.record().state)) {
            return &slot;
        }
    }
    return nullptr;
}

const MakerPaperMarketEngine::Slot* MakerPaperMarketEngine::active_slot(
    std::uint64_t instrument_handle, Side side) const noexcept {
    for (const auto& slot : slots_) {
        if (slot.occupied && slot.oms.record().instrument_handle == instrument_handle
            && slot.oms.record().side == side && queue_active(slot.oms.record().state)) {
            return &slot;
        }
    }
    return nullptr;
}

std::int64_t MakerPaperMarketEngine::available_to_sell(
    std::uint64_t instrument_handle) const noexcept {
    std::int64_t available = is_yes(instrument_handle)
        ? inventory_.yes_microunits : inventory_.no_microunits;
    for (const auto& slot : slots_) {
        if (!slot.occupied || slot.oms.record().instrument_handle != instrument_handle
            || slot.oms.record().side != Side::Sell || !queue_active(slot.oms.record().state)) {
            continue;
        }
        available -= std::max<std::int64_t>(0, slot.oms.record().remaining_microunits);
    }
    return std::max<std::int64_t>(0, available);
}

void MakerPaperMarketEngine::refresh_inventory_derived() noexcept {
    std::int64_t yes_reserved = 0;
    std::int64_t no_reserved = 0;
    for (const auto& slot : slots_) {
        if (!slot.occupied || slot.oms.record().side != Side::Sell
            || !queue_active(slot.oms.record().state)) {
            continue;
        }
        auto& reserved = is_yes(slot.oms.record().instrument_handle)
            ? yes_reserved : no_reserved;
        const auto remaining = std::max<std::int64_t>(0, slot.oms.record().remaining_microunits);
        if (reserved <= std::numeric_limits<std::int64_t>::max() - remaining) {
            reserved += remaining;
        } else {
            reserved = std::numeric_limits<std::int64_t>::max();
        }
    }
    inventory_.yes_reserved_sell_microunits = yes_reserved;
    inventory_.no_reserved_sell_microunits = no_reserved;
    inventory_.yes_free_microunits = std::max<std::int64_t>(
        0, inventory_.yes_microunits - yes_reserved);
    inventory_.no_free_microunits = std::max<std::int64_t>(
        0, inventory_.no_microunits - no_reserved);
    inventory_.balanced_complete_set_microunits = std::min(
        inventory_.yes_microunits, inventory_.no_microunits);
    inventory_.directional_microunits =
        inventory_.yes_microunits - inventory_.no_microunits;
    inventory_.collateral_committed_microdollars = collateral_microdollars(
        inventory_.yes_cost, inventory_.no_cost);
}

QueueEnvelope MakerPaperMarketEngine::make_queue(std::int64_t visible) const noexcept {
    visible = std::max<std::int64_t>(0, visible);
    QueueEnvelope queue;
    queue.ahead_lower_microunits = visible;
    queue.ahead_expected_microunits = scaled_queue(visible, policy_.queue_expected_multiplier);
    queue.ahead_upper_microunits = scaled_queue(visible, policy_.queue_upper_multiplier);
    queue.ahead_expected_microunits = std::max(queue.ahead_expected_microunits,
                                               queue.ahead_lower_microunits);
    queue.ahead_upper_microunits = std::max(queue.ahead_upper_microunits,
                                            queue.ahead_expected_microunits);
    queue.confidence = policy_.queue_confidence;
    return queue;
}

void MakerPaperMarketEngine::emit(PaperMakerResult& result, PaperMakerEvent event) const noexcept {
    if (result.event_count >= result.events.size()) {
        result.invariant_violation = 1;
        return;
    }
    result.events[result.event_count++] = event;
}

OmsEvent MakerPaperMarketEngine::oms_event(OmsEventType type,
                                            std::int64_t timestamp_ns) noexcept {
    OmsEvent event;
    event.event_id = ++oms_event_sequence_;
    event.source_version = ++source_version_;
    event.type = type;
    event.timestamp_ns = timestamp_ns;
    return event;
}

void MakerPaperMarketEngine::request_cancel(
    Slot& slot, std::int64_t now, PaperMakerResult& result) noexcept {
    if (!queue_active(slot.oms.record().state)) return;
    if (slot.oms.record().state == OrderState::CancelRequested
        || slot.oms.record().state == OrderState::CancelPending) {
        return;
    }
    auto request = oms_event(OmsEventType::RequestCancel, now);
    auto transition = slot.oms.apply(request);
    if (transition.invariant_violation) {
        result.invariant_violation = 1;
        return;
    }
    auto wire = oms_event(OmsEventType::WireCancel, now);
    transition = slot.oms.apply(wire);
    if (transition.invariant_violation) {
        result.invariant_violation = 1;
        return;
    }
    if (now > std::numeric_limits<std::int64_t>::max() - policy_.cancel_latency_ns) {
        result.invariant_violation = 1;
        return;
    }
    slot.paper.cancel_effective_monotonic_ns = now + policy_.cancel_latency_ns;
    PaperMakerEvent event;
    event.kind = PaperMakerEventKind::CancelRequested;
    event.order_id = slot.oms.record().client_order_id;
    event.intent_id = slot.intent_id;
    event.instrument_handle = slot.oms.record().instrument_handle;
    event.side = slot.oms.record().side;
    event.order_state = slot.oms.record().state;
    event.timestamp_ns = now;
    event.price_tick = slot.oms.record().price_tick;
    event.tick_size_e4 = slot.tick_size_e4;
    event.original_microunits = slot.oms.record().original_microunits;
    event.queue = slot.paper.queue;
    emit(result, event);
    result.applied = 1;
}

void MakerPaperMarketEngine::apply_operational_fill(
    Slot& slot, std::int64_t fill_microunits, std::int64_t timestamp_ns,
    PaperMakerEvent& event) noexcept {
    if (fill_microunits <= 0) return;
    const auto instrument = slot.oms.record().instrument_handle;
    const Side side = slot.oms.record().side;
    const double fill_shares = shares(fill_microunits);
    const double fill_price = price(slot.oms.record().price_tick, slot.tick_size_e4);
    if (!(fill_price > 0.0 && fill_price < 1.0)) return;

    std::int64_t& quantity = is_yes(instrument)
        ? inventory_.yes_microunits : inventory_.no_microunits;
    double& cost = is_yes(instrument) ? inventory_.yes_cost : inventory_.no_cost;

    double realized = 0.0;
    if (side == Side::Buy) {
        quantity += fill_microunits;
        cost += fill_shares * fill_price;
    } else if (side == Side::Sell) {
        const double current_shares = shares(quantity);
        if (fill_microunits > quantity || current_shares <= 0.0) return;
        const double avg_cost = cost / current_shares;
        realized = fill_shares * (fill_price - avg_cost);
        quantity -= fill_microunits;
        cost = std::max(0.0, cost - fill_shares * avg_cost);
        inventory_.realized_trading_pnl += realized;
    }
    event.realized_pnl = realized;
    event.timestamp_ns = timestamp_ns;
}

void MakerPaperMarketEngine::maybe_merge(
    std::int64_t timestamp_ns, PaperMakerResult& result) noexcept {
    // Do not consume shares reserved by live SELL quotes. Automatic PAPER merge
    // is accounting convenience only; it must never manufacture uncovered sell
    // inventory after a merge.
    const std::int64_t free_complete_sets = std::min(
        available_to_sell(yes_instrument_handle_),
        available_to_sell(no_instrument_handle_));
    const std::int64_t merge_microunits = std::max<std::int64_t>(
        0, free_complete_sets - inventory_.complete_set_reserve_floor_microunits);
    if (merge_microunits <= 0) return;
    const double yes_shares = shares(inventory_.yes_microunits);
    const double no_shares = shares(inventory_.no_microunits);
    if (yes_shares <= 0.0 || no_shares <= 0.0) return;
    const double avg_yes = inventory_.yes_cost / yes_shares;
    const double avg_no = inventory_.no_cost / no_shares;
    const double merged_shares = shares(merge_microunits);
    const double pnl = merged_shares * (1.0 - avg_yes - avg_no);

    PaperMakerEvent event;
    event.kind = PaperMakerEventKind::InventoryMerge;
    event.timestamp_ns = timestamp_ns;
    event.operational_fill_microunits = merge_microunits;
    event.realized_pnl = pnl;
    event.yes_before_microunits = inventory_.yes_microunits;
    event.no_before_microunits = inventory_.no_microunits;
    event.yes_cost_before = inventory_.yes_cost;
    event.no_cost_before = inventory_.no_cost;
    event.collateral_before_microdollars = collateral_microdollars(
        inventory_.yes_cost, inventory_.no_cost);

    inventory_.pending_merge_microunits = merge_microunits;
    inventory_.yes_microunits -= merge_microunits;
    inventory_.no_microunits -= merge_microunits;
    inventory_.yes_cost = std::max(0.0, inventory_.yes_cost - merged_shares * avg_yes);
    inventory_.no_cost = std::max(0.0, inventory_.no_cost - merged_shares * avg_no);
    inventory_.realized_trading_pnl += pnl;
    inventory_.pending_merge_microunits = 0;
    refresh_inventory_derived();
    event.yes_after_microunits = inventory_.yes_microunits;
    event.no_after_microunits = inventory_.no_microunits;
    event.yes_cost_after = inventory_.yes_cost;
    event.no_cost_after = inventory_.no_cost;
    event.collateral_after_microdollars = inventory_.collateral_committed_microdollars;
    emit(result, event);
}

PaperMakerResult MakerPaperMarketEngine::apply_intent(
    const StrategyIntent& intent,
    std::int64_t visible_queue_ahead_microunits,
    std::int32_t tick_size_e4) noexcept {

    PaperMakerResult result;
    auto reject = [&]() noexcept {
        result.rejected = 1;
        PaperMakerEvent event;
        event.kind = PaperMakerEventKind::Rejected;
        event.intent_id = intent.intent_id;
        event.instrument_handle = intent.instrument_handle;
        event.side = intent.side;
        event.timestamp_ns = intent.decision_monotonic_ns;
        event.price_tick = intent.price_tick;
        event.tick_size_e4 = tick_size_e4;
        event.original_microunits = intent.quantity_microunits;
        emit(result, event);
        return result;
    };

    if (!policy_.valid() || market_handle_ == 0 || yes_instrument_handle_ == 0
        || no_instrument_handle_ == 0 || yes_instrument_handle_ == no_instrument_handle_
        || intent.market_handle != market_handle_
        || !instrument_known(intent.instrument_handle)
        || intent.strategy_id != StrategyId::ProfessionalMaker
        || intent.decision_monotonic_ns <= 0 || intent.exchange_event_ns <= 0) {
        return reject();
    }

    if (intent.type == IntentType::CancelQuote) {
        if (intent.side == Side::None) return reject();
        Slot* slot = active_slot(intent.instrument_handle, intent.side);
        if (slot == nullptr) return result;
        request_cancel(*slot, intent.decision_monotonic_ns, result);
        return result;
    }

    if (intent.type == IntentType::Withdraw || intent.type == IntentType::Kill) {
        const bool market_wide = intent.type == IntentType::Kill;
        for (auto& slot : slots_) {
            if (!slot.occupied || !queue_active(slot.oms.record().state)
                || (!market_wide && slot.oms.record().instrument_handle != intent.instrument_handle)) {
                continue;
            }
            request_cancel(slot, intent.decision_monotonic_ns, result);
        }
        result.applied = 1;
        return result;
    }

    if (intent.type != IntentType::Quote || intent.side == Side::None
        || intent.price_tick <= 0 || intent.quantity_microunits <= 0
        || intent.passive != 1 || intent.post_only != 1
        || tick_size_e4 <= 0 || tick_size_e4 > 10'000 || 10'000 % tick_size_e4 != 0
        || !(price(intent.price_tick, tick_size_e4) > 0.0
             && price(intent.price_tick, tick_size_e4) < 1.0)) {
        return reject();
    }
    if (active_slot(intent.instrument_handle, intent.side) != nullptr) return reject();
    if (intent.side == Side::Sell
        && intent.quantity_microunits > available_to_sell(intent.instrument_handle)) {
        return reject();
    }

    Slot* slot = free_slot();
    if (slot == nullptr) return reject();
    *slot = Slot{};
    slot->occupied = 1;
    slot->intent_id = intent.intent_id;
    slot->tick_size_e4 = tick_size_e4;
    const std::uint64_t order_id = ++order_sequence_;
    slot->oms = OmsOrder(intent, order_id);

    if (intent.decision_monotonic_ns
        > std::numeric_limits<std::int64_t>::max() - policy_.assumed_submission_latency_ns) {
        slot->occupied = 0;
        result.invariant_violation = 1;
        return reject();
    }
    const std::int64_t arrival_receive = intent.decision_monotonic_ns
        + policy_.assumed_submission_latency_ns;
    if (arrival_receive <= 0) {
        slot->occupied = 0;
        return reject();
    }

    auto transition = slot->oms.apply(oms_event(OmsEventType::QueueSend,
                                                intent.decision_monotonic_ns));
    if (transition.invariant_violation) {
        slot->occupied = 0;
        result.invariant_violation = 1;
        return reject();
    }
    transition = slot->oms.apply(oms_event(OmsEventType::WireSend, arrival_receive));
    if (transition.invariant_violation) {
        slot->occupied = 0;
        result.invariant_violation = 1;
        return reject();
    }
    auto ack = oms_event(OmsEventType::AckLive, arrival_receive);
    ack.exchange_order_handle = static_cast<std::int64_t>(order_id);
    transition = slot->oms.apply(ack);
    if (transition.invariant_violation || transition.state != OrderState::Live) {
        slot->occupied = 0;
        result.invariant_violation = 1;
        return reject();
    }

    slot->paper.order_id = order_id;
    slot->paper.instrument_handle = intent.instrument_handle;
    slot->paper.side = intent.side;
    slot->paper.price_tick = intent.price_tick;
    slot->paper.original_microunits = intent.quantity_microunits;
    slot->paper.arrival_exchange_event_ns = intent.exchange_event_ns;
    slot->paper.arrival_receive_monotonic_ns = arrival_receive;
    slot->paper.queue = make_queue(visible_queue_ahead_microunits);
    slot->paper.remaining_pessimistic_microunits = intent.quantity_microunits;
    slot->paper.remaining_expected_microunits = intent.quantity_microunits;
    slot->paper.remaining_optimistic_microunits = intent.quantity_microunits;
    if (!slot->paper.valid()) {
        slot->occupied = 0;
        result.invariant_violation = 1;
        return reject();
    }

    PaperMakerEvent event;
    event.kind = PaperMakerEventKind::OrderLive;
    event.order_id = order_id;
    event.intent_id = intent.intent_id;
    event.instrument_handle = intent.instrument_handle;
    event.side = intent.side;
    event.order_state = slot->oms.record().state;
    event.timestamp_ns = arrival_receive;
    event.price_tick = intent.price_tick;
    event.tick_size_e4 = tick_size_e4;
    event.original_microunits = intent.quantity_microunits;
    event.queue = slot->paper.queue;
    emit(result, event);
    refresh_inventory_derived();
    result.applied = 1;
    return result;
}

PaperMakerResult MakerPaperMarketEngine::on_public_trade(
    const PublicTradePrint& trade) noexcept {
    PaperMakerResult result;
    if (!policy_.valid() || !trade.valid() || !instrument_known(trade.instrument_handle)) {
        result.rejected = 1;
        return result;
    }
    if (trade_seen(trade.trade_id)) {
        result.duplicate_trade = 1;
        return result;
    }
    remember_trade(trade.trade_id);

    std::array<PaperRestingOrder, kMaxPaperOrdersPerMarket> fifo{};
    std::array<std::size_t, kMaxPaperOrdersPerMarket> slot_indices{};
    std::size_t count = 0;
    for (std::size_t i = 0; i < slots_.size(); ++i) {
        const auto& slot = slots_[i];
        if (!slot.occupied || !queue_active(slot.oms.record().state)
            || slot.oms.record().instrument_handle != trade.instrument_handle) {
            continue;
        }
        ++result.active_orders_seen;
        const bool causal = trade.exchange_event_ns > slot.paper.arrival_exchange_event_ns
            && trade.receive_monotonic_ns > slot.paper.arrival_receive_monotonic_ns;
        if (!causal) {
            ++result.causally_pre_arrival;
        } else if (slot.paper.cancel_effective_before(trade.receive_monotonic_ns)) {
            ++result.cancel_effective_before_trade;
        } else {
            const bool correct_side = slot.oms.record().side == Side::Buy
                ? trade.aggressor_side == Side::Sell
                : trade.aggressor_side == Side::Buy;
            const bool price_crossing = slot.oms.record().side == Side::Buy
                ? trade.price_tick <= slot.oms.record().price_tick
                : trade.price_tick >= slot.oms.record().price_tick;
            if (!correct_side) ++result.wrong_aggressor_side;
            else if (!price_crossing) ++result.price_not_crossing;
            else ++result.eligible_orders;
        }
        fifo[count] = slot.paper;
        slot_indices[count] = i;
        ++count;
    }
    if (count == 0) {
        result.applied = 1;
        return result;
    }

    // The common queue allocator requires deterministic arrival order.
    for (std::size_t i = 1; i < count; ++i) {
        auto order = fifo[i];
        auto index = slot_indices[i];
        std::size_t j = i;
        while (j > 0) {
            const auto& previous = fifo[j - 1];
            const bool before = order.arrival_receive_monotonic_ns < previous.arrival_receive_monotonic_ns
                || (order.arrival_receive_monotonic_ns == previous.arrival_receive_monotonic_ns
                    && order.arrival_exchange_event_ns < previous.arrival_exchange_event_ns);
            if (!before) break;
            fifo[j] = fifo[j - 1];
            slot_indices[j] = slot_indices[j - 1];
            --j;
        }
        fifo[j] = order;
        slot_indices[j] = index;
    }

    std::array<PaperFillEnvelope, kMaxPaperOrdersPerMarket> fills{};
    auto allocation = allocate_public_print(
        trade,
        std::span<PaperRestingOrder>(fifo.data(), count),
        std::span<PaperFillEnvelope>(fills.data(), count));
    if (allocation.invalid_input) {
        result.invariant_violation = 1;
        return result;
    }
    if (result.eligible_orders > 0) {
        result.eligible_fill_microunits = trade.quantity_microunits;
    }
    result.operational_fill_microunits = allocation.pessimistic_own_fill_microunits;
    std::uint32_t operationally_filled_orders = 0;
    for (std::size_t i = 0; i < allocation.output_count; ++i) {
        if (fills[i].pessimistic_fill_microunits > 0) ++operationally_filled_orders;
    }
    result.queue_not_depleted = result.eligible_orders > operationally_filled_orders
        ? result.eligible_orders - operationally_filled_orders : 0;

    for (std::size_t i = 0; i < count; ++i) {
        slots_[slot_indices[i]].paper = fifo[i];
    }

    for (std::size_t i = 0; i < allocation.output_count; ++i) {
        const auto& fill = fills[i];
        Slot* slot = nullptr;
        for (auto& candidate : slots_) {
            if (candidate.occupied && candidate.oms.record().client_order_id == fill.order_id) {
                slot = &candidate;
                break;
            }
        }
        if (slot == nullptr) {
            result.invariant_violation = 1;
            continue;
        }

        PaperMakerEvent event;
        event.kind = PaperMakerEventKind::Fill;
        event.order_id = fill.order_id;
        event.intent_id = slot->intent_id;
        event.trade_id = fill.trade_id;
        event.instrument_handle = slot->oms.record().instrument_handle;
        event.side = slot->oms.record().side;
        event.price_tick = slot->oms.record().price_tick;
        event.tick_size_e4 = slot->tick_size_e4;
        event.original_microunits = slot->oms.record().original_microunits;
        event.pessimistic_fill_microunits = fill.pessimistic_fill_microunits;
        event.expected_fill_microunits = fill.expected_fill_microunits;
        event.optimistic_fill_microunits = fill.optimistic_fill_microunits;
        event.operational_fill_microunits = fill.pessimistic_fill_microunits;
        event.queue = slot->paper.queue;
        event.timestamp_ns = trade.receive_monotonic_ns;

        if (event.operational_fill_microunits > 0) {
            auto fill_event = oms_event(OmsEventType::FillDelta, trade.receive_monotonic_ns);
            fill_event.fill_delta_microunits = event.operational_fill_microunits;
            const auto transition = slot->oms.apply(fill_event);
            if (transition.invariant_violation) {
                result.invariant_violation = 1;
                continue;
            }
            event.order_state = transition.state;
            apply_operational_fill(*slot, event.operational_fill_microunits,
                                   trade.receive_monotonic_ns, event);
        } else {
            event.order_state = slot->oms.record().state;
        }
        // Canonical evidence must observe the causal fill before any accounting
        // terminal event generated from that fill.
        emit(result, event);
        if (event.operational_fill_microunits > 0) {
            maybe_merge(trade.receive_monotonic_ns, result);
        }
        refresh_inventory_derived();
    }
    result.applied = 1;
    return result;
}

PaperMakerResult MakerPaperMarketEngine::advance_time(
    std::int64_t monotonic_ns) noexcept {
    PaperMakerResult result;
    if (monotonic_ns <= 0 || !policy_.valid()) {
        result.rejected = 1;
        return result;
    }
    for (auto& slot : slots_) {
        if (!slot.occupied || slot.paper.cancel_effective_monotonic_ns <= 0
            || monotonic_ns < slot.paper.cancel_effective_monotonic_ns
            || (slot.oms.record().state != OrderState::CancelRequested
                && slot.oms.record().state != OrderState::CancelPending)) {
            continue;
        }
        const auto transition = slot.oms.apply(oms_event(OmsEventType::AckCancel,
                                                        slot.paper.cancel_effective_monotonic_ns));
        if (transition.invariant_violation) {
            result.invariant_violation = 1;
            continue;
        }
        if (transition.state == OrderState::Cancelled) {
            PaperMakerEvent event;
            event.kind = PaperMakerEventKind::Cancelled;
            event.order_id = slot.oms.record().client_order_id;
            event.intent_id = slot.intent_id;
            event.instrument_handle = slot.oms.record().instrument_handle;
            event.side = slot.oms.record().side;
            event.order_state = transition.state;
            event.timestamp_ns = slot.paper.cancel_effective_monotonic_ns;
            event.price_tick = slot.oms.record().price_tick;
            event.tick_size_e4 = slot.tick_size_e4;
            event.original_microunits = slot.oms.record().original_microunits;
            event.queue = slot.paper.queue;
            emit(result, event);
        }
        result.applied = 1;
    }
    maybe_merge(monotonic_ns, result);
    refresh_inventory_derived();
    return result;
}

QuoteSnapshot MakerPaperMarketEngine::quote_snapshot(
    std::uint64_t instrument_handle) const noexcept {
    QuoteSnapshot out;
    if (!instrument_known(instrument_handle)) return out;
    if (const auto* bid = active_slot(instrument_handle, Side::Buy)) {
        const auto state = bid->oms.record().state;
        out.bid_tick = bid->oms.record().price_tick;
        out.bid_active = static_cast<std::uint8_t>(state == OrderState::Live || state == OrderState::Partial);
        out.cancel_pending = static_cast<std::uint8_t>(
            state == OrderState::CancelRequested || state == OrderState::CancelPending);
        out.last_quote_monotonic_ns = std::max(out.last_quote_monotonic_ns,
                                               bid->oms.record().live_ns);
    }
    if (const auto* ask = active_slot(instrument_handle, Side::Sell)) {
        const auto state = ask->oms.record().state;
        out.ask_tick = ask->oms.record().price_tick;
        out.ask_active = static_cast<std::uint8_t>(state == OrderState::Live || state == OrderState::Partial);
        out.cancel_pending = static_cast<std::uint8_t>(out.cancel_pending
            || state == OrderState::CancelRequested || state == OrderState::CancelPending);
        out.last_quote_monotonic_ns = std::max(out.last_quote_monotonic_ns,
                                               ask->oms.record().live_ns);
    }
    return out;
}

InventorySnapshot MakerPaperMarketEngine::inventory_snapshot() const noexcept {
    InventorySnapshot out;
    out.yes_shares = shares(inventory_.yes_microunits);
    out.no_shares = shares(inventory_.no_microunits);
    for (const auto& slot : slots_) {
        if (!slot.occupied || !queue_active(slot.oms.record().state)) continue;
        const double remaining = shares(slot.oms.record().remaining_microunits);
        const double sign = is_yes(slot.oms.record().instrument_handle) ? 1.0 : -1.0;
        if (slot.oms.record().side == Side::Buy) out.reserved_buy_shares += sign * remaining;
        else if (slot.oms.record().side == Side::Sell) out.reserved_sell_shares += sign * remaining;
    }
    return out;
}

std::size_t MakerPaperMarketEngine::active_order_count() const noexcept {
    std::size_t count = 0;
    for (const auto& slot : slots_) {
        if (slot.occupied && queue_active(slot.oms.record().state)) ++count;
    }
    return count;
}

} // namespace pm::v7::maker
