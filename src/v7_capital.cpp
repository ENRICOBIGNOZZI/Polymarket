#include "pm/v7_capital.hpp"

#include <algorithm>
#include <limits>

namespace pm::v7 {
namespace {

[[nodiscard]] bool add_fits(std::int64_t lhs, std::int64_t rhs, std::int64_t limit) noexcept {
    if (lhs < 0 || rhs < 0 || limit < 0) return false;
    if (rhs > limit) return false;
    return lhs <= limit - rhs;
}

} // namespace

bool CapitalLimits::valid() const noexcept {
    return sleeve_budget_microdollars > 0
        && max_total_exposure_microdollars > 0
        && max_total_exposure_microdollars <= sleeve_budget_microdollars
        && max_market_exposure_microdollars > 0
        && max_market_exposure_microdollars <= max_total_exposure_microdollars
        && max_single_order_microdollars > 0
        && max_single_order_microdollars <= max_market_exposure_microdollars;
}

SleeveCapitalAccount::SleeveCapitalAccount(CapitalLimits limits) noexcept {
    if (limits.valid()) limits_ = limits;
}

bool SleeveCapitalAccount::configure(CapitalLimits limits) noexcept {
    if (!limits.valid()) return false;
    if (order_reserved_microdollars_ != 0 || inventory_committed_microdollars_ != 0) return false;
    limits_ = limits;
    reservations_ = {};
    market_order_reserved_ = {};
    market_inventory_committed_ = {};
    bump_version();
    return true;
}

bool SleeveCapitalAccount::valid_market(std::uint64_t market_handle) const noexcept {
    return market_handle > 0 && market_handle < kMaxCapitalMarkets;
}

bool SleeveCapitalAccount::can_add(std::uint64_t market_handle,
                                   std::int64_t microdollars,
                                   bool apply_single_order_limit) const noexcept {
    if (!limits_.valid() || !valid_market(market_handle) || microdollars <= 0) return false;
    if (apply_single_order_limit && microdollars > limits_.max_single_order_microdollars) return false;

    const auto market = static_cast<std::size_t>(market_handle);
    const std::int64_t current_total = order_reserved_microdollars_ + inventory_committed_microdollars_;
    const std::int64_t market_total = market_order_reserved_[market] + market_inventory_committed_[market];
    return add_fits(current_total, microdollars, limits_.max_total_exposure_microdollars)
        && add_fits(current_total, microdollars, limits_.sleeve_budget_microdollars)
        && add_fits(market_total, microdollars, limits_.max_market_exposure_microdollars);
}

std::size_t SleeveCapitalAccount::hash(std::uint64_t intent_id) const noexcept {
    static_assert((kMaxOpenCapitalReservations & (kMaxOpenCapitalReservations - 1)) == 0,
                  "reservation capacity must be a power of two");
    std::uint64_t x = intent_id + 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27U)) * 0x94d049bb133111ebULL;
    x ^= x >> 31U;
    return static_cast<std::size_t>(x) & (kMaxOpenCapitalReservations - 1U);
}

SleeveCapitalAccount::ReservationSlot* SleeveCapitalAccount::find(
    std::uint64_t intent_id) noexcept {
    if (intent_id == 0) return nullptr;
    const std::size_t start = hash(intent_id);
    for (std::size_t step = 0; step < kMaxOpenCapitalReservations; ++step) {
        auto& slot = reservations_[(start + step) & (kMaxOpenCapitalReservations - 1U)];
        if (slot.state == CapitalReservationState::Empty) return nullptr;
        if (slot.state == CapitalReservationState::Active && slot.intent_id == intent_id) return &slot;
    }
    return nullptr;
}

const SleeveCapitalAccount::ReservationSlot* SleeveCapitalAccount::find(
    std::uint64_t intent_id) const noexcept {
    if (intent_id == 0) return nullptr;
    const std::size_t start = hash(intent_id);
    for (std::size_t step = 0; step < kMaxOpenCapitalReservations; ++step) {
        const auto& slot = reservations_[(start + step) & (kMaxOpenCapitalReservations - 1U)];
        if (slot.state == CapitalReservationState::Empty) return nullptr;
        if (slot.state == CapitalReservationState::Active && slot.intent_id == intent_id) return &slot;
    }
    return nullptr;
}

SleeveCapitalAccount::ReservationSlot* SleeveCapitalAccount::insert_slot(
    std::uint64_t intent_id) noexcept {
    if (intent_id == 0) return nullptr;
    const std::size_t start = hash(intent_id);
    ReservationSlot* first_tombstone = nullptr;
    for (std::size_t step = 0; step < kMaxOpenCapitalReservations; ++step) {
        auto& slot = reservations_[(start + step) & (kMaxOpenCapitalReservations - 1U)];
        if (slot.state == CapitalReservationState::Active && slot.intent_id == intent_id) return &slot;
        if (slot.state == CapitalReservationState::Tombstone && first_tombstone == nullptr) {
            first_tombstone = &slot;
        }
        if (slot.state == CapitalReservationState::Empty) {
            return first_tombstone != nullptr ? first_tombstone : &slot;
        }
    }
    return first_tombstone;
}

void SleeveCapitalAccount::erase(ReservationSlot& slot) noexcept {
    slot.intent_id = 0;
    slot.market_handle = 0;
    slot.microdollars = 0;
    slot.state = CapitalReservationState::Tombstone;
}

void SleeveCapitalAccount::bump_version() noexcept {
    if (state_version_ == std::numeric_limits<std::uint64_t>::max()) state_version_ = 1;
    else ++state_version_;
}

bool SleeveCapitalAccount::reserve_order(std::uint64_t intent_id,
                                         std::uint64_t market_handle,
                                         std::int64_t microdollars) noexcept {
    if (intent_id == 0 || !can_add(market_handle, microdollars, true)) {
        if (const auto* existing = find(intent_id); existing != nullptr) {
            return existing->market_handle == market_handle && existing->microdollars == microdollars;
        }
        return false;
    }

    if (const auto* existing = find(intent_id); existing != nullptr) {
        return existing->market_handle == market_handle && existing->microdollars == microdollars;
    }
    auto* slot = insert_slot(intent_id);
    if (slot == nullptr || slot->state == CapitalReservationState::Active) return false;

    const auto market = static_cast<std::size_t>(market_handle);
    slot->intent_id = intent_id;
    slot->market_handle = market_handle;
    slot->microdollars = microdollars;
    slot->state = CapitalReservationState::Active;
    order_reserved_microdollars_ += microdollars;
    market_order_reserved_[market] += microdollars;
    bump_version();
    return true;
}

bool SleeveCapitalAccount::release_order(std::uint64_t intent_id) noexcept {
    auto* slot = find(intent_id);
    if (slot == nullptr || !valid_market(slot->market_handle) || slot->microdollars <= 0) return false;
    const auto market = static_cast<std::size_t>(slot->market_handle);
    if (order_reserved_microdollars_ < slot->microdollars
        || market_order_reserved_[market] < slot->microdollars) {
        return false;
    }
    order_reserved_microdollars_ -= slot->microdollars;
    market_order_reserved_[market] -= slot->microdollars;
    erase(*slot);
    bump_version();
    return true;
}

bool SleeveCapitalAccount::settle_order_to_inventory(
    std::uint64_t intent_id,
    std::int64_t committed_microdollars) noexcept {
    auto* slot = find(intent_id);
    if (slot == nullptr || !valid_market(slot->market_handle)
        || committed_microdollars < 0 || committed_microdollars > slot->microdollars) {
        return false;
    }
    const auto market = static_cast<std::size_t>(slot->market_handle);
    if (order_reserved_microdollars_ < slot->microdollars
        || market_order_reserved_[market] < slot->microdollars) {
        return false;
    }

    order_reserved_microdollars_ -= slot->microdollars;
    market_order_reserved_[market] -= slot->microdollars;
    inventory_committed_microdollars_ += committed_microdollars;
    market_inventory_committed_[market] += committed_microdollars;
    erase(*slot);
    bump_version();
    return true;
}

bool SleeveCapitalAccount::commit_inventory(std::uint64_t market_handle,
                                            std::int64_t microdollars) noexcept {
    if (!can_add(market_handle, microdollars, false)) return false;
    const auto market = static_cast<std::size_t>(market_handle);
    inventory_committed_microdollars_ += microdollars;
    market_inventory_committed_[market] += microdollars;
    bump_version();
    return true;
}

bool SleeveCapitalAccount::release_inventory(std::uint64_t market_handle,
                                             std::int64_t microdollars) noexcept {
    if (!valid_market(market_handle) || microdollars <= 0) return false;
    const auto market = static_cast<std::size_t>(market_handle);
    if (inventory_committed_microdollars_ < microdollars
        || market_inventory_committed_[market] < microdollars) {
        return false;
    }
    inventory_committed_microdollars_ -= microdollars;
    market_inventory_committed_[market] -= microdollars;
    bump_version();
    return true;
}

std::int64_t SleeveCapitalAccount::order_reserved_for_market(
    std::uint64_t market_handle) const noexcept {
    return valid_market(market_handle)
        ? market_order_reserved_[static_cast<std::size_t>(market_handle)] : 0;
}

std::int64_t SleeveCapitalAccount::inventory_committed_for_market(
    std::uint64_t market_handle) const noexcept {
    return valid_market(market_handle)
        ? market_inventory_committed_[static_cast<std::size_t>(market_handle)] : 0;
}

CapitalSnapshot SleeveCapitalAccount::snapshot() const noexcept {
    CapitalSnapshot out;
    out.order_reserved_microdollars = order_reserved_microdollars_;
    out.inventory_committed_microdollars = inventory_committed_microdollars_;
    out.total_exposure_microdollars = order_reserved_microdollars_ + inventory_committed_microdollars_;
    const std::int64_t ceiling = std::min(limits_.sleeve_budget_microdollars,
                                          limits_.max_total_exposure_microdollars);
    out.available_microdollars = limits_.valid() && out.total_exposure_microdollars <= ceiling
        ? ceiling - out.total_exposure_microdollars : 0;
    out.state_version = state_version_;
    return out;
}

} // namespace pm::v7
