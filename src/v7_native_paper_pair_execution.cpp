#include "pm/v7_native_paper_pair_execution.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7 {
namespace {

[[nodiscard]] bool valid_pair(
    const NativeOrderCommand& yes,
    const NativeOrderCommand& no) noexcept {
    return yes.client_order_id != 0 && no.client_order_id != 0
        && yes.client_order_id != no.client_order_id
        && yes.command_id != 0 && no.command_id != 0
        && yes.market_handle != 0 && yes.market_handle == no.market_handle
        && yes.event_handle == no.event_handle
        && yes.instrument_handle != 0 && no.instrument_handle != 0
        && yes.instrument_handle != no.instrument_handle
        && yes.quantity_microunits > 0
        && yes.quantity_microunits == no.quantity_microunits
        && yes.side == no.side
        && (yes.side == Side::Buy || yes.side == Side::Sell)
        && yes.time_in_force == AdapterTimeInForce::Fok
        && no.time_in_force == AdapterTimeInForce::Fok
        && yes.tick_size_e4 > 0 && no.tick_size_e4 > 0
        && yes.price_tick > 0 && no.price_tick > 0;
}

} // namespace

NativePaperPairExecutionAdapter::NativePaperPairExecutionAdapter(
    NativeSettlementOmsEndpoint& endpoint,
    std::int64_t taker_delay_ns,
    std::int64_t maximum_arrival_book_age_ns) noexcept
    : endpoint_(endpoint),
      taker_delay_ns_(taker_delay_ns),
      maximum_arrival_book_age_ns_(
          maximum_arrival_book_age_ns > 0
              ? maximum_arrival_book_age_ns
              : 100'000'000LL) {}

NativePaperPairExecutionAdapter::Preflight
NativePaperPairExecutionAdapter::preflight(
    const NativeOrderCommand& command,
    const BookHotSnapshot& book) const noexcept {
    Preflight out{};
    out.quantity_microunits = command.quantity_microunits;
    if (command.client_order_id == 0 || command.quantity_microunits <= 0
        || command.price_tick <= 0 || command.tick_size_e4 <= 0
        || command.time_in_force != AdapterTimeInForce::Fok
        || (command.side != Side::Buy && command.side != Side::Sell)) {
        out.reason = NativePaperPairReason::InvalidPair;
        return out;
    }
    if (book.valid == 0 || book.lineage_continuous == 0
        || book.tick_size_e4 != command.tick_size_e4) {
        out.reason = NativePaperPairReason::BookUnavailable;
        return out;
    }

    const auto limit_e4 =
        command.price_tick * static_cast<std::int64_t>(command.tick_size_e4);
    if (limit_e4 <= 0 || limit_e4 >= 10'000) {
        out.reason = NativePaperPairReason::InvalidPair;
        return out;
    }

    const bool buy = command.side == Side::Buy;
    const auto& levels = buy ? book.ask_levels : book.bid_levels;
    const std::size_t count = buy ? book.ask_level_count : book.bid_level_count;
    std::int64_t remaining = command.quantity_microunits;
    long double notional_e4_microunits = 0.0L;
    std::int32_t worst_e4 = 0;
    bool saw_marketable = false;

    for (std::size_t i = 0; i < count && i < levels.size() && remaining > 0; ++i) {
        const auto& level = levels[i];
        if (level.price_e4 <= 0 || level.quantity_microunits <= 0) continue;
        const bool marketable = buy
            ? static_cast<std::int64_t>(level.price_e4) <= limit_e4
            : static_cast<std::int64_t>(level.price_e4) >= limit_e4;
        if (!marketable) break;
        saw_marketable = true;
        const auto take = std::min(remaining, level.quantity_microunits);
        notional_e4_microunits +=
            static_cast<long double>(take) * level.price_e4;
        remaining -= take;
        worst_e4 = level.price_e4;
        out.levels_used = static_cast<std::uint16_t>(i + 1);
    }

    if (!saw_marketable) {
        out.reason = NativePaperPairReason::NotMarketable;
        return out;
    }
    if (remaining != 0) {
        out.reason = NativePaperPairReason::InsufficientDepth;
        return out;
    }
    out.conservative_fill_price_e4 = worst_e4;
    out.vwap_e4 = static_cast<double>(
        notional_e4_microunits
        / static_cast<long double>(command.quantity_microunits));
    out.reason = NativePaperPairReason::Accepted;
    out.fillable = 1;
    return out;
}

bool NativePaperPairExecutionAdapter::reject_pair(
    const NativeOrderCommand& yes,
    const NativeOrderCommand& no,
    std::int64_t timestamp_ns) noexcept {
    if (timestamp_ns <= 0) return false;
    const bool y = endpoint_.observe_unsent(yes, timestamp_ns);
    const bool n = endpoint_.observe_unsent(no, timestamp_ns);
    if (y && n) ++rejected_pairs_;
    return y && n;
}

bool NativePaperPairExecutionAdapter::release_delay(
    const NativeOrderCommand& command,
    std::int64_t timestamp_ns) noexcept {
    OmsEvent elapsed{};
    elapsed.type = OmsEventType::DelayElapsed;
    elapsed.timestamp_ns = timestamp_ns;
    const auto transition =
        endpoint_.apply_owned(command.client_order_id, elapsed);
    return transition.applied != 0
        && transition.invariant_violation == 0
        && transition.reconciliation_required == 0
        && transition.state == OrderState::SendPending;
}

bool NativePaperPairExecutionAdapter::fill_leg(
    const NativeOrderCommand& command,
    const Preflight& preflight,
    std::int64_t now_ns,
    NativePaperPairLegFill& out) noexcept {
    out.command = command;
    out.fill_microunits = command.quantity_microunits;
    out.conservative_fill_price_e4 =
        preflight.conservative_fill_price_e4;
    out.vwap_e4 = preflight.vwap_e4;
    out.levels_used = preflight.levels_used;

    OmsEvent wire{};
    wire.type = OmsEventType::WireSend;
    wire.timestamp_ns = now_ns;
    const auto wired =
        endpoint_.apply_owned(command.client_order_id, wire);
    if (!wired.applied || wired.invariant_violation
        || wired.reconciliation_required
        || wired.state != OrderState::AckPending) {
        return false;
    }

    OmsEvent ack{};
    ack.type = OmsEventType::AckLive;
    ack.timestamp_ns = now_ns + 1;
    ++exchange_sequence_;
    if (exchange_sequence_ == 0) ++exchange_sequence_;
    ack.exchange_order_handle =
        static_cast<std::int64_t>(exchange_sequence_);
    const auto acked =
        endpoint_.apply_owned(command.client_order_id, ack);
    if (!acked.applied || acked.invariant_violation
        || acked.reconciliation_required
        || acked.state != OrderState::Live) {
        return false;
    }

    OmsEvent fill{};
    fill.type = OmsEventType::FillDelta;
    fill.timestamp_ns = now_ns + 2;
    fill.fill_delta_microunits = command.quantity_microunits;
    // Conservative single-price accounting for a multi-level FOK sweep.
    // Exact VWAP is preserved separately in NativePaperPairLegFill.
    fill.fill_price_e4 = preflight.conservative_fill_price_e4;
    const auto filled =
        endpoint_.apply_owned(command.client_order_id, fill);
    if (!filled.applied || filled.invariant_violation
        || filled.reconciliation_required
        || filled.state != OrderState::Filled) {
        return false;
    }
    out.final_state = OrderState::Filled;
    return true;
}

NativePaperPairResult NativePaperPairExecutionAdapter::execute_now(
    const NativeOrderCommand& yes,
    const NativeOrderCommand& no,
    const BookHotSnapshot& yes_book,
    const BookHotSnapshot& no_book,
    std::int64_t now_monotonic_ns) noexcept {
    NativePaperPairResult out{};
    out.yes.command = yes;
    out.no.command = no;
    out.evaluated_arrival_ns = now_monotonic_ns;
    if (!valid_pair(yes, no) || now_monotonic_ns <= 0
        || !endpoint_.matches_pending_command(yes)
        || !endpoint_.matches_pending_command(no)) {
        out.reason = NativePaperPairReason::InvalidPair;
        return out;
    }

    const auto yes_pre = preflight(yes, yes_book);
    const auto no_pre = preflight(no, no_book);
    if (!yes_pre.fillable || !no_pre.fillable) {
        const auto reason =
            !yes_pre.fillable ? yes_pre.reason : no_pre.reason;
        if (!reject_pair(yes, no, now_monotonic_ns)) {
            out.reason = NativePaperPairReason::LifecycleFailure;
            return out;
        }
        out.reason = reason;
        out.yes.final_state = OrderState::Rejected;
        out.no.final_state = OrderState::Rejected;
        return out;
    }

    // Both books are preflighted before the first lifecycle mutation.
    if (!fill_leg(yes, yes_pre, now_monotonic_ns, out.yes)
        || !fill_leg(no, no_pre, now_monotonic_ns + 4, out.no)) {
        out.reason = NativePaperPairReason::LifecycleFailure;
        return out;
    }
    ++complete_pairs_;
    out.reason = NativePaperPairReason::Accepted;
    out.accepted = 1;
    return out;
}

NativePaperPairResult NativePaperPairExecutionAdapter::submit(
    const NativeOrderCommand& yes,
    const NativeOrderCommand& no,
    const BookHotSnapshot& yes_book,
    const BookHotSnapshot& no_book,
    std::int64_t now_monotonic_ns) noexcept {
    NativePaperPairResult out{};
    out.yes.command = yes;
    out.no.command = no;
    if (!valid_pair(yes, no) || now_monotonic_ns <= 0
        || !endpoint_.matches_pending_command(yes)
        || !endpoint_.matches_pending_command(no)) {
        out.reason = NativePaperPairReason::InvalidPair;
        return out;
    }
    if (taker_delay_ns_ < 0) {
        if (!reject_pair(yes, no, now_monotonic_ns)) {
            out.reason = NativePaperPairReason::LifecycleFailure;
            return out;
        }
        out.reason = NativePaperPairReason::VenueTermsUnknown;
        out.censored = 1;
        out.yes.final_state = OrderState::Rejected;
        out.no.final_state = OrderState::Rejected;
        return out;
    }
    if (taker_delay_ns_ == 0) {
        return execute_now(
            yes, no, yes_book, no_book, now_monotonic_ns);
    }
    if (now_monotonic_ns
        > std::numeric_limits<std::int64_t>::max() - taker_delay_ns_) {
        out.reason = NativePaperPairReason::InvalidPair;
        return out;
    }

    PendingPair* slot = nullptr;
    for (auto& pending : pending_) {
        if (pending.yes.client_order_id == 0) {
            slot = &pending;
            break;
        }
    }
    if (slot == nullptr) {
        if (!reject_pair(yes, no, now_monotonic_ns)) {
            out.reason = NativePaperPairReason::LifecycleFailure;
            return out;
        }
        out.reason = NativePaperPairReason::CapacityFull;
        out.yes.final_state = OrderState::Rejected;
        out.no.final_state = OrderState::Rejected;
        return out;
    }

    OmsEvent begin{};
    begin.type = OmsEventType::BeginDelay;
    begin.timestamp_ns = now_monotonic_ns;
    const auto y = endpoint_.apply_owned(yes.client_order_id, begin);
    const auto n = endpoint_.apply_owned(no.client_order_id, begin);
    if (!y.applied || y.state != OrderState::PendingDelay
        || y.invariant_violation || y.reconciliation_required
        || !n.applied || n.state != OrderState::PendingDelay
        || n.invariant_violation || n.reconciliation_required) {
        // If only one leg entered delay, return it to SendPending before
        // rejecting both. Failure of this repair is a hard lifecycle failure.
        bool repair = true;
        if (y.state == OrderState::PendingDelay)
            repair = release_delay(yes, now_monotonic_ns + 1) && repair;
        if (n.state == OrderState::PendingDelay)
            repair = release_delay(no, now_monotonic_ns + 1) && repair;
        if (repair) (void)reject_pair(yes, no, now_monotonic_ns + 2);
        out.reason = NativePaperPairReason::LifecycleFailure;
        return out;
    }

    slot->yes = yes;
    slot->no = no;
    slot->deadline_ns = now_monotonic_ns + taker_delay_ns_;
    slot->invalidated =
        yes_book.valid == 0 || no_book.valid == 0
        || yes_book.lineage_continuous == 0
        || no_book.lineage_continuous == 0;
    out.reason = NativePaperPairReason::PendingArrival;
    out.accepted = 1;
    out.pending_arrival = 1;
    out.scheduled_arrival_ns = slot->deadline_ns;
    return out;
}

NativePaperPairArrivalBatch
NativePaperPairExecutionAdapter::advance_arrivals(
    const BookHotSnapshot& yes_book,
    const BookHotSnapshot& no_book,
    std::int64_t receive_watermark_ns) noexcept {
    NativePaperPairArrivalBatch out{};
    if (receive_watermark_ns <= 0) {
        out.invalid = 1;
        return out;
    }
    for (auto& pending : pending_) {
        if (pending.yes.client_order_id == 0
            || pending.deadline_ns >= receive_watermark_ns) {
            continue;
        }
        if (out.count >= out.records.size()) {
            out.invalid = 1;
            break;
        }
        auto& result = out.records[out.count++];
        result.yes.command = pending.yes;
        result.no.command = pending.no;
        result.scheduled_arrival_ns = pending.deadline_ns;
        result.evaluated_arrival_ns = pending.deadline_ns;

        const bool released =
            release_delay(pending.yes, pending.deadline_ns)
            && release_delay(pending.no, pending.deadline_ns);
        if (!released) {
            result.reason = NativePaperPairReason::LifecycleFailure;
            out.invalid = 1;
            pending = PendingPair{};
            continue;
        }

        const bool unavailable =
            pending.invalidated != 0
            || yes_book.valid == 0 || no_book.valid == 0
            || yes_book.lineage_continuous == 0
            || no_book.lineage_continuous == 0
            || yes_book.receive_monotonic_ns <= 0
            || no_book.receive_monotonic_ns <= 0
            || yes_book.receive_monotonic_ns > pending.deadline_ns
            || no_book.receive_monotonic_ns > pending.deadline_ns
            || pending.deadline_ns - yes_book.receive_monotonic_ns
                > maximum_arrival_book_age_ns_
            || pending.deadline_ns - no_book.receive_monotonic_ns
                > maximum_arrival_book_age_ns_;
        if (unavailable) {
            const bool rejected = reject_pair(
                pending.yes, pending.no, pending.deadline_ns + 1);
            result.reason = rejected
                ? NativePaperPairReason::ArrivalCensored
                : NativePaperPairReason::LifecycleFailure;
            result.censored = rejected ? 1 : 0;
            result.yes.final_state =
                rejected ? OrderState::Rejected : OrderState::Unknown;
            result.no.final_state =
                rejected ? OrderState::Rejected : OrderState::Unknown;
            if (!rejected) out.invalid = 1;
            pending = PendingPair{};
            continue;
        }

        result = execute_now(
            pending.yes, pending.no, yes_book, no_book,
            pending.deadline_ns + 1);
        result.scheduled_arrival_ns = pending.deadline_ns;
        if (result.reason == NativePaperPairReason::LifecycleFailure)
            out.invalid = 1;
        pending = PendingPair{};
    }
    return out;
}

void NativePaperPairExecutionAdapter::invalidate_arrivals() noexcept {
    for (auto& pending : pending_)
        if (pending.yes.client_order_id != 0) pending.invalidated = 1;
}

std::size_t NativePaperPairExecutionAdapter::pending_arrivals() const noexcept {
    std::size_t count = 0;
    for (const auto& pending : pending_)
        count += pending.yes.client_order_id != 0;
    return count;
}

} // namespace pm::v7
