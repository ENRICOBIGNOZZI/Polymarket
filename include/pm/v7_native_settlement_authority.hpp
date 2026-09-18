#pragma once

#include "pm/v7_execution_admission.hpp"
#include "pm/v7_native_order_tx.hpp"

#include <cstdint>

namespace pm::v7 {

enum class NativeSettlementAuthorityReason : std::uint8_t {
    Accepted = 1,
    InvalidPlan = 2,
    BelowVenueMinimum = 3,
    InventoryUnavailable = 4,
    AdmissionDenied = 5,
    OmsDenied = 6,
};

struct NativeSettlementAuthorityResult {
    ExecutionAdmissionResult admission{};
    NativeOrderTxResult tx{};
    NativeSettlementAuthorityReason reason = NativeSettlementAuthorityReason::InvalidPlan;
    std::uint8_t accepted = 0;
    std::uint8_t capital_released_on_failure = 0;
};

// Single in-process owner for the native CRYPTO_SETTLEMENT_ENGINE admission
// chain. Candidate generators never reserve capital or touch the OMS directly.
// The zero-authority convergence runtime starts flat, so SELL new-risk intents
// fail closed until canonical inventory state is wired into this owner.
class NativeSettlementAuthority final {
public:
    explicit NativeSettlementAuthority(CapitalLimits limits) noexcept;
    [[nodiscard]] NativeSettlementAuthorityResult submit(
        const ExecutionPlan& plan,
        std::int64_t minimum_order_microunits,
        std::int64_t now_monotonic_ns) noexcept;

    [[nodiscard]] bool release_capital(std::uint64_t intent_id) noexcept {
        return capital_.release_order(intent_id);
    }
    [[nodiscard]] std::size_t active_orders() const noexcept {
        return order_tx_.active_orders();
    }

private:
    SleeveCapitalAccount capital_;
    NativeOrderTxOwner order_tx_{};
};

} // namespace pm::v7
