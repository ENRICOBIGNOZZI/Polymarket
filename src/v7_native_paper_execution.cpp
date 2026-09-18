#include "pm/v7_native_paper_execution.hpp"

#include <algorithm>

namespace pm::v7 {

NativePaperExecutionAdapter::NativePaperExecutionAdapter(
    NativeSettlementOmsEndpoint& endpoint,
    std::int64_t cancel_latency_ns) noexcept
    : endpoint_(endpoint),
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
        (void)endpoint_.observe_unsent(command, now_monotonic_ns);
        out.reason = NativePaperReason::BookUnavailable;
        return out;
    }
    if (command.time_in_force == AdapterTimeInForce::Gtc && free_slot() == nullptr) {
        (void)endpoint_.observe_unsent(command, now_monotonic_ns);
        out.reason = NativePaperReason::CapacityFull;
        return out;
    }
    if (command.time_in_force != AdapterTimeInForce::Gtc
        && command.time_in_force != AdapterTimeInForce::Fak) {
        (void)endpoint_.observe_unsent(command, now_monotonic_ns);
        out.reason = NativePaperReason::InvalidCommand;
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
    const auto executable_qty = buy ? book.best_ask_microunits : book.best_bid_microunits;
    // Frozen taker semantics are ARRIVAL_BEST_ASK_NO_CHASE / best-bid SELL.
    // Requiring equality keeps capital basis, simulated fill price and ledger
    // economics byte-for-byte aligned with the admitted command.
    const bool marketable = executable_e4 > 0 && executable_e4 == limit_e4;
    if (!marketable || executable_qty < command.quantity_microunits) {
        OmsEvent expire{};
        expire.type = OmsEventType::Expire;
        expire.timestamp_ns = now_monotonic_ns + 2;
        const auto expired = endpoint_.apply_owned(command.client_order_id, expire);
        out.reason = marketable ? NativePaperReason::InsufficientDepth
                                : NativePaperReason::NotMarketable;
        out.final_state = expired.state;
        return out;
    }

    OmsEvent fill{};
    fill.type = OmsEventType::FillDelta;
    fill.timestamp_ns = now_monotonic_ns + 2;
    fill.fill_delta_microunits = command.quantity_microunits;
    const auto filled = endpoint_.apply_owned(command.client_order_id, fill);
    out.final_state = filled.state;
    if (filled.applied == 0 || filled.invariant_violation != 0
        || filled.state != OrderState::Filled) {
        out.reason = NativePaperReason::LifecycleFailure;
        return out;
    }
    ++paper_fills_;
    out.reason = NativePaperReason::Accepted;
    out.filled_microunits = command.quantity_microunits;
    out.fill.command = command;
    out.fill.client_order_id = command.client_order_id;
    out.fill.command_id = command.command_id;
    out.fill.instrument_handle = command.instrument_handle;
    out.fill.side = command.side;
    out.fill.price_tick = command.price_tick;
    out.fill.tick_size_e4 = command.tick_size_e4;
    out.fill.fill_microunits = command.quantity_microunits;
    out.fill.exchange_event_ns = book.exchange_event_ns;
    out.fill.receive_monotonic_ns = now_monotonic_ns;
    out.fill.taker = 1;
    out.accepted = 1;
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

bool NativePaperExecutionAdapter::advance_time(std::int64_t now_monotonic_ns) noexcept {
    if (now_monotonic_ns <= 0) return false;
    bool ok = true;
    for (auto& slot : slots_) {
        if (slot.occupied == 0 || slot.cancel_deadline_ns <= 0
            || now_monotonic_ns < slot.cancel_deadline_ns) continue;
        OmsEvent ack{};
        ack.type = OmsEventType::AckCancel;
        ack.timestamp_ns = slot.cancel_deadline_ns;
        const auto result = endpoint_.apply_owned(slot.client_order_id, ack);
        if (result.applied == 0 || result.invariant_violation != 0
            || result.state != OrderState::Cancelled) {
            ok = false;
            continue;
        }
        ++paper_cancels_;
        clear(slot);
    }
    return ok;
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
