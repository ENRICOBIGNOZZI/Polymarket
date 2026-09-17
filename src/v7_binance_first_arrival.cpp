#include "pm/v7_binance_first_arrival.hpp"

#include <cmath>

namespace pm::v7::external_fair {

bool BinanceAggTradeFirstArrivalGate::valid_event(
    const ExternalVenueEvent& event) noexcept {
    return event.venue == VenueId::BinanceSpot
        && event.event_type == ExternalEventType::Trade
        && event.asset_handle != 0
        && event.source_sequence != 0
        && event.exchange_event_ns > 0
        && event.local_receive_monotonic_ns > 0
        && std::isfinite(event.trade_price)
        && std::isfinite(event.trade_size)
        && event.trade_price > 0.0
        && event.trade_size > 0.0
        && (event.trade_side == 1 || event.trade_side == -1)
        && event.gap == 0
        && event.stale == 0
        && event.healthy != 0;
}

bool BinanceAggTradeFirstArrivalGate::same_payload(
    const Slot& slot, const ExternalVenueEvent& event) noexcept {
    return slot.asset_handle == event.asset_handle
        && slot.source_sequence == event.source_sequence
        && slot.exchange_event_ns == event.exchange_event_ns
        && slot.trade_price == event.trade_price
        && slot.trade_size == event.trade_size
        && slot.trade_side == event.trade_side;
}

BinanceFirstArrivalResult BinanceAggTradeFirstArrivalGate::observe(
    std::uint8_t lane,
    const ExternalVenueEvent& event) noexcept {
    BinanceFirstArrivalResult out{};
    if (lane >= 3 || !valid_event(event)) return out;

    const auto index = static_cast<std::size_t>(event.source_sequence)
        & (kBinanceFirstArrivalSlots - 1);
    auto& slot = slots_[index];
    const auto bit = static_cast<std::uint8_t>(1U << lane);

    if (slot.occupied == 0 || slot.source_sequence != event.source_sequence) {
        if (high_watermark_sequence_ != 0
            && event.source_sequence <= high_watermark_sequence_) {
            ++stale_sequences_;
            out.disposition = BinanceFirstArrivalDisposition::StaleSequence;
            out.source_sequence = event.source_sequence;
            return out;
        }
        high_watermark_sequence_ = event.source_sequence;
        slot = Slot{};
        slot.asset_handle = event.asset_handle;
        slot.source_sequence = event.source_sequence;
        slot.exchange_event_ns = event.exchange_event_ns;
        slot.first_receive_monotonic_ns = event.local_receive_monotonic_ns;
        slot.trade_price = event.trade_price;
        slot.trade_size = event.trade_size;
        slot.trade_side = event.trade_side;
        slot.lane_mask = bit;
        slot.occupied = 1;
        ++first_arrivals_;

        out.event = event;
        out.disposition = BinanceFirstArrivalDisposition::First;
        out.source_sequence = event.source_sequence;
        out.first_receive_monotonic_ns = event.local_receive_monotonic_ns;
        out.lane_mask = bit;
        out.emit_first = 1;
        return out;
    }
    out.event = event;
    out.source_sequence = event.source_sequence;
    out.first_receive_monotonic_ns = slot.first_receive_monotonic_ns;
    out.lane_mask = slot.lane_mask;
    out.independent_confirmations = slot.independent_confirmations;
    out.conflict = slot.conflict;

    if (!same_payload(slot, event)
        || event.local_receive_monotonic_ns < slot.first_receive_monotonic_ns) {
        slot.conflict = 1;
        ++conflicts_;
        out.disposition = BinanceFirstArrivalDisposition::Conflict;
        out.conflict = 1;
        return out;
    }

    if ((slot.lane_mask & bit) != 0) {
        ++duplicates_;
        out.disposition = BinanceFirstArrivalDisposition::SameLaneDuplicate;
        return out;
    }

    slot.lane_mask = static_cast<std::uint8_t>(slot.lane_mask | bit);
    if (slot.independent_confirmations < 2) ++slot.independent_confirmations;
    ++confirms_;

    out.disposition = BinanceFirstArrivalDisposition::IndependentConfirm;
    out.confirmation_receive_monotonic_ns = event.local_receive_monotonic_ns;
    out.lane_mask = slot.lane_mask;
    out.independent_confirmations = slot.independent_confirmations;
    return out;
}

} // namespace pm::v7::external_fair
