#include "pm/v7_market_state.hpp"

#include <algorithm>
#include <bit>
#include <limits>

namespace pm::v7 {
namespace {

using Occupancy = std::array<std::uint64_t, kOccupancyWords>;

void set_occupied(Occupancy& bits, std::int32_t index, bool occupied) noexcept {
    if (index < 0 || static_cast<std::size_t>(index) >= kCanonicalPriceSlots) return;
    const auto word = static_cast<std::size_t>(index) >> 6U;
    const auto bit = static_cast<unsigned>(index) & 63U;
    const std::uint64_t mask = std::uint64_t{1} << bit;
    if (occupied) bits[word] |= mask;
    else bits[word] &= ~mask;
}

[[nodiscard]] std::int32_t find_previous(const Occupancy& bits,
                                         std::int32_t from_exclusive) noexcept {
    if (from_exclusive <= 1) return 0;
    std::int32_t index = std::min<std::int32_t>(
        from_exclusive - 1, static_cast<std::int32_t>(kCanonicalPriceSlots - 1));
    std::size_t word = static_cast<std::size_t>(index) >> 6U;
    unsigned bit = static_cast<unsigned>(index) & 63U;
    std::uint64_t mask = bit == 63U ? std::numeric_limits<std::uint64_t>::max()
                                    : ((std::uint64_t{1} << (bit + 1U)) - 1U);
    std::uint64_t value = bits[word] & mask;
    while (true) {
        if (value != 0) {
            const auto highest = 63U - static_cast<unsigned>(std::countl_zero(value));
            return static_cast<std::int32_t>((word << 6U) + highest);
        }
        if (word == 0) break;
        --word;
        value = bits[word];
    }
    return 0;
}

[[nodiscard]] std::int32_t find_next(const Occupancy& bits,
                                     std::int32_t from_exclusive) noexcept {
    if (from_exclusive >= static_cast<std::int32_t>(kCanonicalPriceSlots - 1)) return 0;
    std::int32_t index = std::max<std::int32_t>(1, from_exclusive + 1);
    std::size_t word = static_cast<std::size_t>(index) >> 6U;
    const unsigned bit = static_cast<unsigned>(index) & 63U;
    std::uint64_t value = bits[word] & (~std::uint64_t{0} << bit);
    while (word < bits.size()) {
        if (value != 0) {
            const auto lowest = static_cast<unsigned>(std::countr_zero(value));
            const auto found = static_cast<std::int32_t>((word << 6U) + lowest);
            return static_cast<std::size_t>(found) < kCanonicalPriceSlots ? found : 0;
        }
        ++word;
        if (word >= bits.size()) break;
        value = bits[word];
    }
    return 0;
}

} // namespace

CanonicalL2Book::CanonicalL2Book(std::int32_t tick_size_e4) noexcept {
    if (!set_tick_size(tick_size_e4)) tick_size_e4_ = 100;
}

bool CanonicalL2Book::set_tick_size(std::int32_t tick_size_e4) noexcept {
    if (tick_size_e4 <= 0 || tick_size_e4 > kCanonicalPriceScale
        || kCanonicalPriceScale % tick_size_e4 != 0) {
        return false;
    }
    for (std::size_t i = 1; i + 1 < kCanonicalPriceSlots; ++i) {
        if ((bid_qty_[i] > 0 || ask_qty_[i] > 0)
            && static_cast<std::int32_t>(i) % tick_size_e4 != 0) {
            return false;
        }
    }
    tick_size_e4_ = tick_size_e4;
    return true;
}

bool CanonicalL2Book::valid_level(std::int32_t price_e4,
                                  std::int64_t quantity_microunits) const noexcept {
    return price_e4 > 0 && price_e4 < kCanonicalPriceScale
        && price_e4 % tick_size_e4_ == 0 && quantity_microunits >= 0;
}

void CanonicalL2Book::clear() noexcept {
    bid_qty_.fill(0);
    ask_qty_.fill(0);
    bid_occupied_.fill(0);
    ask_occupied_.fill(0);
    best_bid_e4_ = 0;
    best_ask_e4_ = 0;
}

void CanonicalL2Book::set_raw(Side side, std::int32_t price_e4,
                              std::int64_t quantity_microunits) noexcept {
    auto& qty = side == Side::Buy ? bid_qty_ : ask_qty_;
    auto& occupied = side == Side::Buy ? bid_occupied_ : ask_occupied_;
    qty[static_cast<std::size_t>(price_e4)] = quantity_microunits;
    set_occupied(occupied, price_e4, quantity_microunits > 0);
}

bool CanonicalL2Book::replace_snapshot(std::span<const PriceLevelE4> bids,
                                       std::span<const PriceLevelE4> asks,
                                       std::int64_t exchange_event_ns,
                                       std::int64_t receive_monotonic_ns) noexcept {
    if (exchange_event_ns <= 0 || receive_monotonic_ns <= 0
        || (receive_monotonic_ns_ > 0 && receive_monotonic_ns < receive_monotonic_ns_)) {
        return false;
    }
    for (const auto& level : bids) {
        if (!valid_level(level.price_e4, level.quantity_microunits)) return false;
    }
    for (const auto& level : asks) {
        if (!valid_level(level.price_e4, level.quantity_microunits)) return false;
    }

    clear();
    for (const auto& level : bids) set_raw(Side::Buy, level.price_e4, level.quantity_microunits);
    for (const auto& level : asks) set_raw(Side::Sell, level.price_e4, level.quantity_microunits);
    best_bid_e4_ = best_bid();
    best_ask_e4_ = best_ask();
    if (best_bid_e4_ <= 0 || best_ask_e4_ <= 0 || best_bid_e4_ >= best_ask_e4_) {
        clear();
        return false;
    }
    exchange_event_ns_ = exchange_event_ns;
    receive_monotonic_ns_ = receive_monotonic_ns;
    ++state_version_;
    return true;
}

bool CanonicalL2Book::mutate_level(Side side, std::int32_t price_e4,
                                   std::int64_t quantity_microunits,
                                   std::int64_t exchange_event_ns,
                                   std::int64_t receive_monotonic_ns) noexcept {
    if (side == Side::None || !valid_level(price_e4, quantity_microunits)
        || exchange_event_ns <= 0 || receive_monotonic_ns <= 0
        || (receive_monotonic_ns_ > 0 && receive_monotonic_ns < receive_monotonic_ns_)) {
        return false;
    }
    if (quantity_microunits > 0) {
        if (side == Side::Buy && best_ask_e4_ > 0 && price_e4 >= best_ask_e4_) return false;
        if (side == Side::Sell && best_bid_e4_ > 0 && price_e4 <= best_bid_e4_) return false;
    }

    set_raw(side, price_e4, quantity_microunits);
    if (side == Side::Buy) {
        if (quantity_microunits > 0 && price_e4 > best_bid_e4_) best_bid_e4_ = price_e4;
        else if (quantity_microunits == 0 && price_e4 == best_bid_e4_) best_bid_e4_ = best_bid();
    } else {
        if (quantity_microunits > 0 && (best_ask_e4_ == 0 || price_e4 < best_ask_e4_)) best_ask_e4_ = price_e4;
        else if (quantity_microunits == 0 && price_e4 == best_ask_e4_) best_ask_e4_ = best_ask();
    }
    exchange_event_ns_ = exchange_event_ns;
    receive_monotonic_ns_ = receive_monotonic_ns;
    ++state_version_;
    return true;
}

std::int32_t CanonicalL2Book::best_bid() const noexcept {
    return find_previous(bid_occupied_, static_cast<std::int32_t>(kCanonicalPriceSlots));
}

std::int32_t CanonicalL2Book::best_ask() const noexcept {
    return find_next(ask_occupied_, 0);
}

std::int32_t CanonicalL2Book::next_bid(std::int32_t from_exclusive) const noexcept {
    return find_previous(bid_occupied_, from_exclusive);
}

std::int32_t CanonicalL2Book::next_ask(std::int32_t from_exclusive) const noexcept {
    return find_next(ask_occupied_, from_exclusive);
}

DepthSummary CanonicalL2Book::depth_summary(Side side) const noexcept {
    DepthSummary out;
    std::int64_t cumulative = 0;
    std::int32_t price = side == Side::Buy ? best_bid_e4_ : best_ask_e4_;
    for (int level = 1; level <= 10 && price > 0; ++level) {
        const auto quantity = quantity_at(side, price);
        cumulative += std::max<std::int64_t>(0, quantity);
        if (level == 1) out.l1_microunits = cumulative;
        if (level == 5) out.l5_microunits = cumulative;
        if (level == 10) out.l10_microunits = cumulative;
        price = side == Side::Buy ? next_bid(price) : next_ask(price);
    }
    if (out.l5_microunits == 0) out.l5_microunits = cumulative;
    if (out.l10_microunits == 0) out.l10_microunits = cumulative;
    return out;
}

BookHotSnapshot CanonicalL2Book::hot_snapshot() const noexcept {
    BookHotSnapshot out;
    out.state_version = state_version_;
    out.exchange_event_ns = exchange_event_ns_;
    out.receive_monotonic_ns = receive_monotonic_ns_;
    out.tick_size_e4 = tick_size_e4_;
    out.best_bid_e4 = best_bid_e4_;
    out.best_ask_e4 = best_ask_e4_;
    out.best_bid_microunits = quantity_at(Side::Buy, best_bid_e4_);
    out.best_ask_microunits = quantity_at(Side::Sell, best_ask_e4_);
    out.bid_depth = depth_summary(Side::Buy);
    out.ask_depth = depth_summary(Side::Sell);
    out.valid = static_cast<std::uint8_t>(
        state_version_ > 0 && exchange_event_ns_ > 0 && receive_monotonic_ns_ > 0
        && best_bid_e4_ > 0 && best_ask_e4_ > best_bid_e4_
        && venue_tick_index(best_bid_e4_) > 0 && venue_tick_index(best_ask_e4_) > 0);
    return out;
}

std::int64_t CanonicalL2Book::quantity_at(Side side, std::int32_t price_e4) const noexcept {
    if (price_e4 <= 0 || static_cast<std::size_t>(price_e4) >= kCanonicalPriceSlots) return 0;
    if (side == Side::Buy) return bid_qty_[static_cast<std::size_t>(price_e4)];
    if (side == Side::Sell) return ask_qty_[static_cast<std::size_t>(price_e4)];
    return 0;
}

std::int32_t CanonicalL2Book::venue_tick_index(std::int32_t price_e4) const noexcept {
    if (price_e4 <= 0 || tick_size_e4_ <= 0 || price_e4 % tick_size_e4_ != 0) return -1;
    return price_e4 / tick_size_e4_;
}

} // namespace pm::v7
