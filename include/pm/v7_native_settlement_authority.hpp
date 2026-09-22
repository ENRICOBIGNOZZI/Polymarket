#pragma once

#include "pm/v7_execution_admission.hpp"
#include "pm/v7_native_order_tx.hpp"

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7 {

inline constexpr std::size_t kNativeInventoryCapacity = 512;
static_assert((kNativeInventoryCapacity & (kNativeInventoryCapacity - 1)) == 0);

enum class NativeSettlementAuthorityReason : std::uint8_t {
    Accepted = 1,
    InvalidPlan = 2,
    BelowVenueMinimum = 3,
    InventoryUnavailable = 4,
    AdmissionDenied = 5,
    OmsDenied = 6,
    DuplicateMakerQuote = 7,
    MakerReplacePending = 8,
    InventoryInvariant = 9,
    LifecycleInvariant = 10,
};

struct NativeSettlementAuthorityResult {
    ExecutionAdmissionResult admission{};
    NativeOrderTxResult tx{};
    NativeCancelTxResult cancel{};
    NativeSettlementAuthorityReason reason = NativeSettlementAuthorityReason::InvalidPlan;
    std::uint8_t accepted = 0;
    std::uint8_t capital_released_on_failure = 0;
};

struct NativeInventorySnapshot {
    std::uint64_t market_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    std::int64_t total_microunits = 0;
    std::int64_t reserved_sell_microunits = 0;
    std::int64_t available_microunits = 0;
    std::int64_t collateral_basis_microdollars = 0;
};

struct NativeLifecycleResult {
    OmsOrderRecord record{}; // Exact authority record before terminal slot retirement.
    OmsTransitionResult transition{};
    NativeInventorySnapshot inventory{};
    NativeSettlementAuthorityReason reason = NativeSettlementAuthorityReason::LifecycleInvariant;
    std::uint8_t applied = 0;
    std::uint8_t terminal_retired = 0;
};

enum class NativeSettlementPairReason : std::uint8_t {
    Accepted = 1,
    InvalidPair = 2,
    FirstLegRejected = 3,
    SecondLegRejectedRolledBack = 4,
    RollbackFailed = 5,
};

struct NativeSettlementPairResult {
    NativeSettlementAuthorityResult yes{};
    NativeSettlementAuthorityResult no{};
    NativeLifecycleResult rollback{};
    NativeSettlementPairReason reason = NativeSettlementPairReason::InvalidPair;
    std::int64_t risk_admitted_monotonic_ns = 0;
    std::uint8_t accepted = 0;
    std::uint8_t rollback_complete = 0;
};


// Single in-process owner for the native CRYPTO_SETTLEMENT_ENGINE admission,
// inventory, maker lifecycle and OMS chain. Candidate generators never reserve
// capital, reserve inventory, mutate OMS state or select a second executor.
class NativeSettlementAuthority final {
public:
    explicit NativeSettlementAuthority(CapitalLimits limits) noexcept;

    // Cold/recovery boundary. Synchronization is rejected while the instrument
    // has an active native order so a stale snapshot cannot overwrite a fill or
    // a live sell reservation.
    [[nodiscard]] bool sync_inventory(std::uint64_t market_handle,
                                      std::uint64_t instrument_handle,
                                      std::int64_t total_microunits,
                                      std::int64_t collateral_basis_microdollars,
                                      std::uint64_t state_version) noexcept;

    [[nodiscard]] NativeSettlementAuthorityResult submit(
        const ExecutionPlan& plan,
        std::int64_t minimum_order_microunits,
        std::int64_t now_monotonic_ns) noexcept;

    // Complete-set pair admission is all-or-none before any transport call.
    // Both legs must be dedicated PureArbFok plans with identical market,
    // side and quantity. If the second leg cannot be admitted, the first leg
    // is rejected and all capital/inventory reservations are rolled back.
    [[nodiscard]] NativeSettlementPairResult submit_pair(
        const ExecutionPlan& yes_plan,
        const ExecutionPlan& no_plan,
        std::int64_t minimum_order_microunits,
        std::int64_t now_monotonic_ns) noexcept;

    // Maker control traffic remains inside the same owner. A replacement quote
    // is never admitted in parallel with the old quote: submit() first requests
    // cancellation and returns MakerReplacePending. The strategy may submit the
    // fresh quote only after a terminal cancel event retires the prior quote.
    [[nodiscard]] NativeCancelTxResult cancel_maker_quote(
        std::uint64_t instrument_handle,
        Side side,
        std::int64_t now_monotonic_ns) noexcept;

    // Sole mutation entry point for adapter/reconciliation order events. Fill
    // deltas update canonical inventory and the common capital account before a
    // terminal order is retired.
    [[nodiscard]] NativeLifecycleResult apply_order_event(
        std::uint64_t client_order_id,
        OmsEvent event) noexcept;

    // Transport may consume exactly the command admitted by this authority.
    // A matching client ID alone is insufficient to bind price, size or ticks.
    [[nodiscard]] bool matches_pending_command(const NativeOrderCommand& command) const noexcept;

    [[nodiscard]] NativeInventorySnapshot inventory_snapshot(
        std::uint64_t instrument_handle) const noexcept;
    // Read-only view of the sole OMS; no strategy-side quote/inventory copies.
    [[nodiscard]] const OmsOrderRecord* maker_order(
        std::uint64_t instrument_handle, Side side) const noexcept {
        const auto* inv = inventory(instrument_handle);
        if (inv == nullptr || (side != Side::Buy && side != Side::Sell)) return nullptr;
        const auto id = side == Side::Buy ? inv->maker_buy_client_order_id : inv->maker_sell_client_order_id;
        return id != 0 ? order_tx_.find(id) : nullptr;
    }
    [[nodiscard]] CapitalSnapshot capital_snapshot() const noexcept {
        return capital_.snapshot();
    }
    [[nodiscard]] const OmsOrderRecord* find_order(
        std::uint64_t client_order_id) const noexcept {
        return order_tx_.find(client_order_id);
    }
    [[nodiscard]] std::size_t active_orders() const noexcept {
        return order_tx_.active_orders();
    }

private:
    struct InventorySlot {
        std::uint64_t market_handle = 0;
        std::uint64_t instrument_handle = 0;
        std::uint64_t state_version = 0;
        std::uint64_t maker_buy_client_order_id = 0;
        std::uint64_t maker_sell_client_order_id = 0;
        std::int64_t total_microunits = 0;
        std::int64_t reserved_sell_microunits = 0;
        std::int64_t collateral_basis_microdollars = 0;
        std::uint8_t occupied = 0;
    };

    struct OrderTrack {
        NativeOrderCommand admitted_command{};
        std::uint64_t client_order_id = 0;
        std::uint64_t intent_id = 0;
        std::uint64_t market_handle = 0;
        std::uint64_t instrument_handle = 0;
        std::int64_t price_e4 = 0;
        std::int64_t settled_buy_basis_microdollars = 0;
        std::int64_t reserved_sell_microunits = 0;
        Side side = Side::None;
        std::uint8_t maker_quote = 0;
        std::uint8_t occupied = 0;
    };

    [[nodiscard]] std::size_t inventory_hash(std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] InventorySlot* inventory(std::uint64_t instrument_handle) noexcept;
    [[nodiscard]] const InventorySlot* inventory(std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] InventorySlot* ensure_inventory(std::uint64_t market_handle,
                                                  std::uint64_t instrument_handle) noexcept;
    [[nodiscard]] bool instrument_has_active_order(std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] OrderTrack* track(std::uint64_t client_order_id) noexcept;
    [[nodiscard]] const OrderTrack* track(std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] bool price_e4(const ExecutionPlan& plan, std::int64_t& out) const noexcept;
    [[nodiscard]] bool cumulative_buy_basis(const OrderTrack& item,
                                            std::int64_t cumulative_fill_microunits,
                                            std::int64_t& out) const noexcept;
    [[nodiscard]] bool apply_fill(OrderTrack& item,
                                  InventorySlot& inv,
                                  std::int64_t fill_delta_microunits,
                                  std::int64_t cumulative_fill_microunits,
                                  std::int32_t actual_fill_price_e4) noexcept;
    void clear_maker_quote(const OrderTrack& item) noexcept;
    void bump_inventory_version(InventorySlot& inv) noexcept;

    SleeveCapitalAccount capital_;
    NativeOrderTxOwner order_tx_{};
    std::array<InventorySlot, kNativeInventoryCapacity> inventory_{};
    std::array<OrderTrack, kNativeOrderTxCapacity> order_tracks_{};
};

} // namespace pm::v7
