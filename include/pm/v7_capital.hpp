#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7 {

inline constexpr std::size_t kMaxCapitalMarkets = 256;
inline constexpr std::size_t kMaxOpenCapitalReservations = 1024;

enum class CapitalReservationState : std::uint8_t {
    Empty = 0,
    Active = 1,
    Tombstone = 2,
};

struct CapitalLimits {
    std::int64_t sleeve_budget_microdollars = 0;
    std::int64_t max_total_exposure_microdollars = 0;
    std::int64_t max_market_exposure_microdollars = 0;
    std::int64_t max_single_order_microdollars = 0;

    [[nodiscard]] bool valid() const noexcept;
};

struct CapitalSnapshot {
    std::int64_t order_reserved_microdollars = 0;
    std::int64_t inventory_committed_microdollars = 0;
    std::int64_t total_exposure_microdollars = 0;
    std::int64_t available_microdollars = 0;
    std::uint64_t state_version = 0;
};

// Common V7 single-writer pre-trade capital plane.
//
// The portfolio/execution owner feeds this class a frozen sleeve allowance.  It
// does not choose strategy weights and therefore cannot become a second capital
// allocator.  The implementation is fixed-capacity, allocation-free, mutex-free
// and intended to be owned by exactly one arbitration / order-TX thread.
class SleeveCapitalAccount final {
public:
    explicit SleeveCapitalAccount(CapitalLimits limits = {}) noexcept;

    [[nodiscard]] bool configure(CapitalLimits limits) noexcept;
    [[nodiscard]] const CapitalLimits& limits() const noexcept { return limits_; }

    // Idempotent for the same active intent_id/market/amount tuple.  A repeated
    // intent with mismatched economics is rejected rather than double-reserved.
    [[nodiscard]] bool reserve_order(std::uint64_t intent_id,
                                     std::uint64_t market_handle,
                                     std::int64_t microdollars) noexcept;
    [[nodiscard]] bool release_order(std::uint64_t intent_id) noexcept;

    // Convert a live-order reservation into inventory capital after a fill.  The
    // committed cost must not exceed the reservation.  Partial fills can be
    // settled by reserving each child order separately; the common OMS owns the
    // exact lifecycle and calls this only on an economically final transition.
    [[nodiscard]] bool settle_order_to_inventory(std::uint64_t intent_id,
                                                 std::int64_t committed_microdollars) noexcept;

    // Explicit non-order transformations such as PAPER complete-set split use
    // inventory commitment directly.  The caller must release the same amount
    // when merge/settlement converts the inventory back to collateral.
    [[nodiscard]] bool commit_inventory(std::uint64_t market_handle,
                                        std::int64_t microdollars) noexcept;
    [[nodiscard]] bool release_inventory(std::uint64_t market_handle,
                                         std::int64_t microdollars) noexcept;

    [[nodiscard]] std::int64_t order_reserved_for_market(
        std::uint64_t market_handle) const noexcept;
    [[nodiscard]] std::int64_t inventory_committed_for_market(
        std::uint64_t market_handle) const noexcept;
    [[nodiscard]] CapitalSnapshot snapshot() const noexcept;

private:
    struct ReservationSlot {
        std::uint64_t intent_id = 0;
        std::uint64_t market_handle = 0;
        std::int64_t microdollars = 0;
        CapitalReservationState state = CapitalReservationState::Empty;
    };

    [[nodiscard]] bool valid_market(std::uint64_t market_handle) const noexcept;
    [[nodiscard]] bool can_add(std::uint64_t market_handle,
                               std::int64_t microdollars,
                               bool apply_single_order_limit) const noexcept;
    [[nodiscard]] std::size_t hash(std::uint64_t intent_id) const noexcept;
    [[nodiscard]] ReservationSlot* find(std::uint64_t intent_id) noexcept;
    [[nodiscard]] const ReservationSlot* find(std::uint64_t intent_id) const noexcept;
    [[nodiscard]] ReservationSlot* insert_slot(std::uint64_t intent_id) noexcept;
    void erase(ReservationSlot& slot) noexcept;
    void bump_version() noexcept;

    CapitalLimits limits_{};
    std::array<ReservationSlot, kMaxOpenCapitalReservations> reservations_{};
    std::array<std::int64_t, kMaxCapitalMarkets> market_order_reserved_{};
    std::array<std::int64_t, kMaxCapitalMarkets> market_inventory_committed_{};
    std::int64_t order_reserved_microdollars_ = 0;
    std::int64_t inventory_committed_microdollars_ = 0;
    std::uint64_t state_version_ = 1;
};

} // namespace pm::v7
