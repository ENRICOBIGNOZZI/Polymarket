#include "pm/v7_native_settlement_authority.hpp"

namespace pm::v7 {

NativeSettlementAuthority::NativeSettlementAuthority(CapitalLimits limits) noexcept
    : capital_(limits) {}

NativeSettlementAuthorityResult NativeSettlementAuthority::submit(
    const ExecutionPlan& plan,
    std::int64_t minimum_order_microunits,
    std::int64_t now_monotonic_ns) noexcept {
    NativeSettlementAuthorityResult out;
    const auto& intent = plan.intent;
    if (minimum_order_microunits <= 0 || intent.quantity_microunits <= 0
        || plan.tick_size_e4 <= 0 || now_monotonic_ns <= 0) {
        out.reason = NativeSettlementAuthorityReason::InvalidPlan;
        return out;
    }
    if (intent.quantity_microunits < minimum_order_microunits) {
        out.reason = NativeSettlementAuthorityReason::BelowVenueMinimum;
        return out;
    }
    // Flat zero-authority convergence state has no filled inventory to sell.
    // Production inventory synchronization is a mandatory gate before cutover.
    if (intent.side == Side::Sell) {
        out.reason = NativeSettlementAuthorityReason::InventoryUnavailable;
        return out;
    }

    out.admission = ExecutionAdmission::admit(intent, plan.tick_size_e4, capital_);
    if (out.admission.accepted == 0) {
        out.reason = NativeSettlementAuthorityReason::AdmissionDenied;
        return out;
    }
    out.tx = order_tx_.prepare_submit(plan, now_monotonic_ns);
    if (out.tx.accepted == 0 || out.tx.oms.state != OrderState::SendPending) {
        if (out.admission.capital_reserved != 0) {
            out.capital_released_on_failure = capital_.release_order(intent.intent_id) ? 1 : 0;
        }
        out.reason = NativeSettlementAuthorityReason::OmsDenied;
        return out;
    }
    out.reason = NativeSettlementAuthorityReason::Accepted;
    out.accepted = 1;
    return out;
}

} // namespace pm::v7
