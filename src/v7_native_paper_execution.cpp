#include "pm/v7_native_paper_execution.hpp"

#include <algorithm>
#include <limits>

namespace pm::v7 {

NativePaperExecutionAdapter::NativePaperExecutionAdapter(
    NativeSettlementOmsEndpoint& endpoint,
    std::int64_t cancel_latency_ns, std::int64_t taker_delay_ns,
    std::int64_t maximum_arrival_book_age_ns) noexcept
    : endpoint_(endpoint), taker_delay_ns_(taker_delay_ns),
      maximum_arrival_book_age_ns_(maximum_arrival_book_age_ns),
      cancel_latency_ns_(cancel_latency_ns > 0 ? cancel_latency_ns : 100'000'000LL) {}

NativePaperExecutionAdapter::Slot* NativePaperExecutionAdapter::free_slot() noexcept {
    for (auto& slot : slots_) if (slot.occupied == 0) return &slot;
    return nullptr;
}

NativePaperExecutionAdapter::Slot* NativePaperExecutionAdapter::find(
    std::uint64_t client_order_id) noexcept {
    if (client_order_id == 0) return nullptr;
    for (auto& slot : slots_) {
        if (slot.occupied != 0 && slot.client_order_id == client_order_id) return &slot;
    }
    return nullptr;
}

void NativePaperExecutionAdapter::clear(Slot& slot) noexcept { slot = Slot{}; }

std::int64_t NativePaperExecutionAdapter::visible_queue(
    const NativeOrderCommand& command,
    const BookHotSnapshot& book) const noexcept {
    const auto px = command.price_tick * static_cast<std::int64_t>(command.tick_size_e4);
    const auto& levels = command.side == Side::Buy ? book.bid_levels : book.ask_levels;
    const auto count = command.side == Side::Buy ? book.bid_level_count : book.ask_level_count;
    for (std::size_t i = 0; i < count && i < levels.size(); ++i) {
        if (levels[i].price_e4 == px) return std::max<std::int64_t>(0, levels[i].quantity_microunits);
    }
    return 0;
}

bool NativePaperExecutionAdapter::live_locally(
    const NativeOrderCommand& command,
    std::int64_t now_monotonic_ns) noexcept {
    OmsEvent wire{};
    wire.type = OmsEventType::WireSend;
    wire.timestamp_ns = now_monotonic_ns;
    const auto wire_result = endpoint_.apply_owned(command.client_order_id, wire);
    if (wire_result.applied == 0 || wire_result.invariant_violation != 0
        || wire_result.state != OrderState::AckPending) return false;

    OmsEvent ack{};
    ack.type = OmsEventType::AckLive;
    ack.timestamp_ns = now_monotonic_ns + 1;
    ++synthetic_exchange_sequence_;
    if (synthetic_exchange_sequence_ == 0) ++synthetic_exchange_sequence_;
    ack.exchange_order_handle = static_cast<std::int64_t>(synthetic_exchange_sequence_);
    const auto ack_result = endpoint_.apply_owned(command.client_order_id, ack);
    if (ack_result.applied == 0 || ack_result.invariant_violation != 0
        || ack_result.state != OrderState::Live) return false;
    ++synthetic_acks_;
    return true;
}

NativePaperSubmitResult NativePaperExecutionAdapter::submit(
    const NativeOrderCommand& command, const BookHotSnapshot& book,
    std::int64_t now_monotonic_ns) noexcept {
    if (command.time_in_force != AdapterTimeInForce::Fak || taker_delay_ns_ == 0)
        return match_now(command, book, now_monotonic_ns);
    NativePaperSubmitResult out{};
    out.client_order_id = command.client_order_id;
    if (now_monotonic_ns <= 0 || !endpoint_.matches_pending_command(command)) return out;
    if (taker_delay_ns_ < 0 || maximum_arrival_book_age_ns_ <= 0
        || now_monotonic_ns > std::numeric_limits<std::int64_t>::max() - taker_delay_ns_ - 3) {
        out.reason = endpoint_.observe_unsent(command, now_monotonic_ns)
            ? NativePaperReason::VenueTermsUnknown : NativePaperReason::LifecycleFailure;
        out.final_state = OrderState::Rejected;
        out.censored = 1;
        return out;
    }
    for (const auto& pending : pending_) {
        if (pending.command.client_order_id == command.client_order_id) return out;
    }
    for (auto& pending : pending_) {
        if (pending.command.client_order_id != 0) continue;
        OmsEvent delay{};
        delay.type = OmsEventType::BeginDelay;
        delay.timestamp_ns = now_monotonic_ns;
        const auto transition = endpoint_.apply_owned(command.client_order_id, delay);
        if (!transition.applied || transition.invariant_violation
            || transition.reconciliation_required
            || transition.state != OrderState::PendingDelay) {
            out.reason = NativePaperReason::LifecycleFailure;
            out.final_state = OrderState::Unknown;
            return out;
        }
        pending.command = command;
        pending.deadline_ns = now_monotonic_ns + taker_delay_ns_;
        pending.invalidated = book.valid == 0 || book.lineage_continuous == 0;
        out.accepted = 1;
        out.pending_arrival = 1;
        out.final_state = OrderState::PendingDelay;
        out.reason = NativePaperReason::PendingArrival;
        return out;
    }
    out.reason = endpoint_.observe_unsent(command, now_monotonic_ns)
        ? NativePaperReason::CapacityFull : NativePaperReason::LifecycleFailure;
    out.final_state = OrderState::Rejected;
    return out;
}

NativePaperPairResult NativePaperExecutionAdapter::submit_pair_fok(
    const NativeOrderCommand& yes_command,
    const BookHotSnapshot& yes_book,
    const NativeOrderCommand& no_command,
    const BookHotSnapshot& no_book,
    std::int64_t now_monotonic_ns) noexcept {
    NativePaperPairResult out{};
    out.yes.client_order_id = yes_command.client_order_id;
    out.no.client_order_id = no_command.client_order_id;

    const auto executable = [](const NativeOrderCommand& command,
                               const BookHotSnapshot& book,
                               std::int32_t& price_e4) noexcept {
        price_e4 = 0;
        if (command.time_in_force != AdapterTimeInForce::Fok
            || command.client_order_id == 0
            || command.quantity_microunits <= 0
            || command.tick_size_e4 <= 0
            || (command.side != Side::Buy && command.side != Side::Sell)
            || book.valid == 0 || book.lineage_continuous == 0
            || book.tick_size_e4 != command.tick_size_e4) return false;
        const auto limit = command.price_tick
            * static_cast<std::int64_t>(command.tick_size_e4);
        const bool buy = command.side == Side::Buy;
        const auto price = buy ? book.best_ask_e4 : book.best_bid_e4;
        const auto depth = buy ? book.best_ask_microunits
                               : book.best_bid_microunits;
        if (price <= 0 || price % command.tick_size_e4 != 0
            || depth < command.quantity_microunits
            || !(buy ? price <= limit : price >= limit)) return false;
        price_e4 = price;
        return true;
    };

    const bool pair_semantics =
        now_monotonic_ns > 0
        && yes_command.market_handle != 0
        && yes_command.market_handle == no_command.market_handle
        && yes_command.event_handle == no_command.event_handle
        && yes_command.instrument_handle != 0
        && no_command.instrument_handle != 0
        && yes_command.instrument_handle != no_command.instrument_handle
        && yes_command.side == no_command.side
        && yes_command.quantity_microunits == no_command.quantity_microunits
        && endpoint_.matches_pending_command(yes_command)
        && endpoint_.matches_pending_command(no_command);
    std::int32_t yes_price = 0, no_price = 0;
    const bool yes_full = pair_semantics
        && executable(yes_command, yes_book, yes_price);
    const bool no_full = pair_semantics
        && executable(no_command, no_book, no_price);

    if (!pair_semantics || !yes_full || !no_full) {
        const auto reason = !pair_semantics
            ? NativePaperReason::InvalidCommand
            : (!yes_full || !no_full)
                ? NativePaperReason::InsufficientDepth
                : NativePaperReason::NotMarketable;
        const bool ry = endpoint_.observe_unsent(
            yes_command, now_monotonic_ns);
        const bool rn = endpoint_.observe_unsent(
            no_command, now_monotonic_ns);
        out.yes.reason = reason;
        out.no.reason = reason;
        out.yes.final_state = ry ? OrderState::Rejected : OrderState::Unknown;
        out.no.final_state = rn ? OrderState::Rejected : OrderState::Unknown;
        out.invalid = (ry && rn) ? 0 : 1;
        return out;
    }

    if (!live_locally(yes_command, now_monotonic_ns)
        || !live_locally(no_command, now_monotonic_ns + 1)) {
        out.invalid = 1;
        out.yes.reason = NativePaperReason::LifecycleFailure;
        out.no.reason = NativePaperReason::LifecycleFailure;
        return out;
    }

    OmsEvent yes_fill{};
    yes_fill.type = OmsEventType::FillDelta;
    yes_fill.timestamp_ns = now_monotonic_ns + 2;
    yes_fill.fill_delta_microunits = yes_command.quantity_microunits;
    yes_fill.fill_price_e4 = yes_price;
    const auto yes_result = endpoint_.apply_owned(
        yes_command.client_order_id, yes_fill);

    OmsEvent no_fill{};
    no_fill.type = OmsEventType::FillDelta;
    no_fill.timestamp_ns = now_monotonic_ns + 3;
    no_fill.fill_delta_microunits = no_command.quantity_microunits;
    no_fill.fill_price_e4 = no_price;
    const auto no_result = endpoint_.apply_owned(
        no_command.client_order_id, no_fill);

    const bool yes_ok = yes_result.applied != 0
        && yes_result.invariant_violation == 0
        && yes_result.state == OrderState::Filled;
    const bool no_ok = no_result.applied != 0
        && no_result.invariant_violation == 0
        && no_result.state == OrderState::Filled;
    out.one_leg_fill = static_cast<std::uint8_t>(yes_ok != no_ok);
    if (!yes_ok || !no_ok) {
        out.invalid = 1;
        out.yes.reason = NativePaperReason::LifecycleFailure;
        out.no.reason = NativePaperReason::LifecycleFailure;
        out.yes.final_state = yes_result.state;
        out.no.final_state = no_result.state;
        return out;
    }

    const auto fill_record = [](const NativeOrderCommand& command,
                                const BookHotSnapshot& book,
                                std::int32_t execution_e4,
                                std::int64_t fill_ns) noexcept {
        NativePaperFillRecord fill{};
        fill.command = command;
        fill.strategy_id = StrategyId::HardArbitrage;
        fill.client_order_id = command.client_order_id;
        fill.command_id = command.command_id;
        fill.instrument_handle = command.instrument_handle;
        fill.side = command.side;
        fill.price_tick = execution_e4 / command.tick_size_e4;
        fill.tick_size_e4 = command.tick_size_e4;
        fill.fill_microunits = command.quantity_microunits;
        fill.exchange_event_ns = book.exchange_event_ns;
        fill.receive_monotonic_ns = fill_ns;
        fill.order_state = OrderState::Filled;
        fill.taker = 1;
        fill.arrival_book_receive_ns = book.receive_monotonic_ns;
        fill.arrival_book_version = book.state_version;
        fill.causal_arrival_modelled = 0;
        return fill;
    };

    out.yes.reason = NativePaperReason::Accepted;
    out.yes.final_state = OrderState::Filled;
    out.yes.filled_microunits = yes_command.quantity_microunits;
    out.yes.fill = fill_record(
        yes_command, yes_book, yes_price, now_monotonic_ns + 2);
    out.yes.accepted = 1;

    out.no.reason = NativePaperReason::Accepted;
    out.no.final_state = OrderState::Filled;
    out.no.filled_microunits = no_command.quantity_microunits;
    out.no.fill = fill_record(
        no_command, no_book, no_price, now_monotonic_ns + 3);
    out.no.accepted = 1;

    paper_fills_ += 2;
    out.paired_fill = 1;
    out.accepted = 1;
    return out;
}

void NativePaperExecutionAdapter::invalidate_arrivals() noexcept {
    for (auto& pending : pending_) if (pending.command.client_order_id) pending.invalidated = 1;
}

std::size_t NativePaperExecutionAdapter::pending_arrivals() const noexcept {
    std::size_t count = 0;
    for (const auto& pending : pending_) count += pending.command.client_order_id != 0;
    return count;
}

NativePaperExecutionAdapter::ConsumedTop* NativePaperExecutionAdapter::available_top(
    const NativeOrderCommand& command, const BookHotSnapshot& book) noexcept {
    const bool buy = command.side == Side::Buy;
    const auto price = buy ? book.best_ask_e4 : book.best_bid_e4;
    const auto visible = std::max<std::int64_t>(0, buy ? book.best_ask_microunits : book.best_bid_microunits);
    ConsumedTop* empty = nullptr;
    for (auto& level : consumed_tops_) {
        if (level.instrument == 0 && empty == nullptr) empty = &level;
        if (level.instrument != command.instrument_handle || level.side != command.side || level.price_e4 != price) continue;
        // A new version alone is not replenishment. Infer only the visible
        // increment, retaining depletion across multiple simulated orders.
        const auto increase = std::max<std::int64_t>(0, visible - level.visible);
        level.remaining = std::min(visible, level.remaining) + std::min(increase, visible - std::min(visible, level.remaining));
        level.visible = visible;
        return &level;
    }
    if (!empty) return nullptr;
    *empty = {command.instrument_handle, command.side, price, visible, visible};
    return empty;
}

NativePaperArrivalBatch NativePaperExecutionAdapter::advance_arrivals(
    std::uint64_t instrument, const BookHotSnapshot& previous_book,
    std::int64_t receive_watermark_ns) noexcept {
    NativePaperArrivalBatch out{};
    if (instrument == 0 || receive_watermark_ns <= 0) { out.invalid = 1; return out; }
    for (auto& pending : pending_) {
        if (pending.command.client_order_id == 0 || pending.command.instrument_handle != instrument
            || pending.deadline_ns >= receive_watermark_ns) continue;
        auto& record = out.records[out.count++];
        record.command = pending.command;
        OmsEvent elapsed{};
        elapsed.type = OmsEventType::DelayElapsed;
        elapsed.timestamp_ns = pending.deadline_ns;
        const auto delay_transition = endpoint_.apply_owned(
            pending.command.client_order_id, elapsed);
        if (!delay_transition.applied || delay_transition.invariant_violation
            || delay_transition.reconciliation_required
            || delay_transition.state != OrderState::SendPending) {
            record.result.client_order_id = pending.command.client_order_id;
            record.result.reason = NativePaperReason::LifecycleFailure;
            record.result.final_state = OrderState::Unknown;
            out.invalid = 1;
            pending = PendingArrival{};
            continue;
        }
        const bool unavailable = pending.invalidated || previous_book.valid == 0
            || previous_book.lineage_continuous == 0 || previous_book.receive_monotonic_ns <= 0
            || previous_book.receive_monotonic_ns > pending.deadline_ns
            || pending.deadline_ns - previous_book.receive_monotonic_ns > maximum_arrival_book_age_ns_;
        if (unavailable) {
            record.result.client_order_id = pending.command.client_order_id;
            record.result.reason = endpoint_.observe_unsent(pending.command, pending.deadline_ns)
                ? NativePaperReason::ArrivalCensored : NativePaperReason::LifecycleFailure;
            record.result.final_state = OrderState::Rejected;
            record.result.censored = 1;
        } else {
            record.result = match_now(pending.command, previous_book, pending.deadline_ns);
        }
        if (record.result.reason == NativePaperReason::LifecycleFailure
            || record.result.reason == NativePaperReason::InvalidCommand) out.invalid = 1;
        pending = PendingArrival{};
    }
    return out;
}

NativePaperSubmitResult NativePaperExecutionAdapter::match_now(
    const NativeOrderCommand& command,
    const BookHotSnapshot& book,
    std::int64_t now_monotonic_ns) noexcept {
    NativePaperSubmitResult out{};
    out.client_order_id = command.client_order_id;
    if (now_monotonic_ns <= 0 || !endpoint_.matches_pending_command(command)) {
        out.reason = NativePaperReason::InvalidCommand;
        return out;
    }
    if (book.valid == 0 || book.lineage_continuous == 0
        || book.tick_size_e4 != command.tick_size_e4) {
        if (!endpoint_.observe_unsent(command, now_monotonic_ns)) {
            out.reason = NativePaperReason::LifecycleFailure;
            return out;
        }
        out.reason = NativePaperReason::BookUnavailable;
        out.final_state = OrderState::Rejected;
        return out;
    }
    if (command.time_in_force == AdapterTimeInForce::Gtc && free_slot() == nullptr) {
        if (!endpoint_.observe_unsent(command, now_monotonic_ns)) {
            out.reason = NativePaperReason::LifecycleFailure;
            return out;
        }
        out.reason = NativePaperReason::CapacityFull;
        out.final_state = OrderState::Rejected;
        return out;
    }
    if (command.time_in_force != AdapterTimeInForce::Gtc
        && command.time_in_force != AdapterTimeInForce::Fak) {
        if (!endpoint_.observe_unsent(command, now_monotonic_ns)) {
            out.reason = NativePaperReason::LifecycleFailure;
            return out;
        }
        out.reason = NativePaperReason::InvalidCommand;
        out.final_state = OrderState::Rejected;
        return out;
    }
    if (!live_locally(command, now_monotonic_ns)) {
        out.reason = NativePaperReason::LifecycleFailure;
        return out;
    }

    if (command.time_in_force == AdapterTimeInForce::Gtc) {
        auto* slot = free_slot();
        if (slot == nullptr) {
            out.reason = NativePaperReason::CapacityFull;
            return out;
        }
        slot->command = command;
        slot->client_order_id = command.client_order_id;
        slot->command_id = command.command_id;
        slot->tick_size_e4 = command.tick_size_e4;
        slot->occupied = 1;
        slot->paper.order_id = command.client_order_id;
        slot->paper.instrument_handle = command.instrument_handle;
        slot->paper.side = command.side;
        slot->paper.price_tick = command.price_tick;
        slot->paper.original_microunits = command.quantity_microunits;
        slot->paper.arrival_exchange_event_ns = std::max<std::int64_t>(1, book.exchange_event_ns);
        slot->paper.arrival_receive_monotonic_ns = now_monotonic_ns;
        const auto queue = visible_queue(command, book);
        slot->paper.queue.ahead_lower_microunits = queue;
        slot->paper.queue.ahead_expected_microunits = queue;
        slot->paper.queue.ahead_upper_microunits = queue;
        slot->paper.queue.confidence = 1.0;
        slot->paper.remaining_pessimistic_microunits = command.quantity_microunits;
        slot->paper.remaining_expected_microunits = command.quantity_microunits;
        slot->paper.remaining_optimistic_microunits = command.quantity_microunits;
        out.reason = NativePaperReason::Accepted;
        out.final_state = OrderState::Live;
        out.accepted = 1;
        out.resting = 1;
        return out;
    }

    const auto limit_e4 = command.price_tick * static_cast<std::int64_t>(command.tick_size_e4);
    const bool buy = command.side == Side::Buy;
    const auto executable_e4 = buy ? book.best_ask_e4 : book.best_bid_e4;
    auto* depth = taker_delay_ns_ > 0 ? available_top(command, book) : nullptr;
    const auto executable_qty = taker_delay_ns_ > 0
        ? (depth ? depth->remaining : 0)
        : (buy ? book.best_ask_microunits : book.best_bid_microunits);
    // FAK is marketable at the arrival top when it is at or better than the
    // submitted limit. Price improvement is real execution, not a non-fill.
    const bool marketable = executable_e4 > 0
        && (buy ? executable_e4 <= limit_e4 : executable_e4 >= limit_e4);
    if (!marketable || executable_qty <= 0) {
        OmsEvent expire{};
        expire.type = OmsEventType::Expire;
        expire.timestamp_ns = now_monotonic_ns + 2;
        const auto expired = endpoint_.apply_owned(command.client_order_id, expire);
        out.reason = marketable ? NativePaperReason::InsufficientDepth
                                : NativePaperReason::NotMarketable;
        if (taker_delay_ns_ > 0 && depth == nullptr) {
            out.reason = NativePaperReason::DepthAccountingUnavailable;
            out.censored = 1;
        }
        out.final_state = expired.state;
        return out;
    }

    const auto fill_qty = std::min(command.quantity_microunits, executable_qty);
    if (executable_e4 % command.tick_size_e4 != 0) {
        out.reason = NativePaperReason::LifecycleFailure;
        return out;
    }
    OmsEvent fill{};
    fill.type = OmsEventType::FillDelta;
    fill.timestamp_ns = now_monotonic_ns + 2;
    fill.fill_delta_microunits = fill_qty;
    fill.fill_price_e4 = static_cast<std::int32_t>(executable_e4);
    const auto filled = endpoint_.apply_owned(command.client_order_id, fill);
    if (filled.applied == 0 || filled.invariant_violation != 0
        || (filled.state != OrderState::Filled && filled.state != OrderState::Partial)) {
        out.reason = NativePaperReason::LifecycleFailure;
        return out;
    }
    if (depth) depth->remaining -= fill_qty;
    ++paper_fills_;
    out.reason = fill_qty == command.quantity_microunits
        ? NativePaperReason::Accepted : NativePaperReason::PartialFillModelled;
    out.filled_microunits = fill_qty;
    out.fill.command = command;
    out.fill.client_order_id = command.client_order_id;
    out.fill.command_id = command.command_id;
    out.fill.instrument_handle = command.instrument_handle;
    out.fill.side = command.side;
    out.fill.price_tick = executable_e4 / command.tick_size_e4;
    out.fill.tick_size_e4 = command.tick_size_e4;
    out.fill.fill_microunits = fill_qty;
    out.fill.exchange_event_ns = book.exchange_event_ns;
    out.fill.receive_monotonic_ns = now_monotonic_ns;
    out.fill.order_state = filled.state;
    out.fill.taker = 1;
    out.fill.arrival_book_receive_ns = book.receive_monotonic_ns;
    out.fill.arrival_book_version = book.state_version;
    out.fill.causal_arrival_modelled = taker_delay_ns_ > 0;
    out.accepted = 1;

    if (fill_qty < command.quantity_microunits) {
        OmsEvent expire{};
        expire.type = OmsEventType::Expire;
        expire.timestamp_ns = now_monotonic_ns + 3;
        const auto expired = endpoint_.apply_owned(command.client_order_id, expire);
        if (expired.applied == 0 || expired.invariant_violation != 0
            || expired.state != OrderState::Expired) {
            out.reason = NativePaperReason::LifecycleFailure;
            out.accepted = 0;
            return out;
        }
        out.final_state = expired.state;
    } else {
        out.final_state = filled.state;
    }
    return out;
}

bool NativePaperExecutionAdapter::request_cancel(
    const NativeCancelCommand& command,
    std::int64_t now_monotonic_ns) noexcept {
    auto* slot = find(command.client_order_id);
    const auto* record = endpoint_.find(command.client_order_id);
    if (slot == nullptr || record == nullptr || now_monotonic_ns <= 0
        || record->state != OrderState::CancelRequested
        || command.exchange_order_handle == 0
        || record->exchange_order_handle != command.exchange_order_handle) return false;

    OmsEvent wire{};
    wire.type = OmsEventType::WireCancel;
    wire.timestamp_ns = now_monotonic_ns;
    const auto transition = endpoint_.apply_owned(command.client_order_id, wire);
    if (transition.applied == 0 || transition.invariant_violation != 0
        || transition.state != OrderState::CancelPending) return false;
    slot->cancel_deadline_ns = now_monotonic_ns + cancel_latency_ns_;
    slot->paper.cancel_effective_monotonic_ns = slot->cancel_deadline_ns;
    return true;
}

NativePaperAdvanceResult NativePaperExecutionAdapter::advance_time(
    std::int64_t now_monotonic_ns) noexcept {
    NativePaperAdvanceResult out{};
    if (now_monotonic_ns <= 0) {
        out.invalid = 1;
        return out;
    }
    for (auto& slot : slots_) {
        if (slot.occupied == 0 || slot.cancel_deadline_ns <= 0
            || now_monotonic_ns < slot.cancel_deadline_ns) continue;
        OmsEvent ack{};
        ack.type = OmsEventType::AckCancel;
        ack.timestamp_ns = slot.cancel_deadline_ns;
        const auto result = endpoint_.apply_owned(slot.client_order_id, ack);
        if (result.applied == 0 || result.invariant_violation != 0
            || result.state != OrderState::Cancelled
            || out.cancellation_count >= out.cancellations.size()) {
            out.invalid = 1;
            continue;
        }
        auto& record = out.cancellations[out.cancellation_count++];
        record.command = slot.command;
        record.cancel_effective_monotonic_ns = slot.cancel_deadline_ns;
        ++paper_cancels_;
        clear(slot);
    }
    return out;
}

NativePaperTradeResult NativePaperExecutionAdapter::on_public_trade(
    const PublicTradePrint& trade) noexcept {
    NativePaperTradeResult out{};
    if (!trade.valid()) {
        out.invalid = 1;
        return out;
    }
    std::array<PaperRestingOrder, kNativePaperOrderCapacity> working{};
    std::array<std::size_t, kNativePaperOrderCapacity> mapping{};
    std::size_t count = 0;
    for (std::size_t i = 0; i < slots_.size(); ++i) {
        if (slots_[i].occupied == 0
            || slots_[i].paper.instrument_handle != trade.instrument_handle) continue;
        working[count] = slots_[i].paper;
        mapping[count] = i;
        ++count;
    }
    if (count == 0) return out;
    const auto allocated = allocate_public_print(
        trade,
        std::span<PaperRestingOrder>(working.data(), count),
        std::span<PaperFillEnvelope>(fill_scratch_.data(), count));
    if (allocated.invalid_input != 0) {
        out.invalid = 1;
        return out;
    }
    for (std::size_t i = 0; i < count; ++i) slots_[mapping[i]].paper = working[i];
    for (std::size_t i = 0; i < allocated.output_count; ++i) {
        const auto fill_qty = fill_scratch_[i].pessimistic_fill_microunits;
        if (fill_qty <= 0) continue;
        auto* slot = find(fill_scratch_[i].order_id);
        if (slot == nullptr) {
            out.invalid = 1;
            continue;
        }
        OmsEvent fill{};
        fill.type = OmsEventType::FillDelta;
        fill.timestamp_ns = trade.receive_monotonic_ns;
        fill.fill_delta_microunits = fill_qty;
        const auto result = endpoint_.apply_owned(slot->client_order_id, fill);
        if (result.applied == 0 || result.invariant_violation != 0) {
            out.invalid = 1;
            continue;
        }
        if (out.fills >= out.records.size()) {
            out.invalid = 1;
            break;
        }
        auto& record = out.records[out.fills];
        record.command = slot->command;
        record.client_order_id = slot->client_order_id;
        record.command_id = slot->command_id;
        record.instrument_handle = trade.instrument_handle;
        record.side = slot->paper.side;
        record.price_tick = slot->paper.price_tick;
        record.tick_size_e4 = slot->tick_size_e4;
        record.fill_microunits = fill_qty;
        record.exchange_event_ns = trade.exchange_event_ns;
        record.receive_monotonic_ns = trade.receive_monotonic_ns;
        record.order_state = result.state;
        record.taker = 0;
        ++out.fills;
        out.filled_microunits += fill_qty;
        ++paper_fills_;
        if (result.state == OrderState::Filled) clear(*slot);
    }
    return out;
}

std::size_t NativePaperExecutionAdapter::resting_orders() const noexcept {
    std::size_t count = 0;
    for (const auto& slot : slots_) if (slot.occupied != 0) ++count;
    return count;
}

} // namespace pm::v7
