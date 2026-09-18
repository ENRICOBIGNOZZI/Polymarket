#pragma once
#include "pm/v7_native_settlement_authority.hpp"

namespace pm::v7 {

// Non-owning adapter endpoint. Every transport event passes through the one
// settlement authority, including capital/inventory updates and retirement.
// The retained terminal record is immutable evidence, never another OMS.
class NativeSettlementOmsEndpoint final {
public:
    explicit NativeSettlementOmsEndpoint(NativeSettlementAuthority& authority) noexcept
        : authority_(authority) {}

    [[nodiscard]] const OmsOrderRecord* find(std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] OmsTransitionResult apply_owned(std::uint64_t client_order_id,
                                                 OmsEvent event) noexcept;
    // Diagnostic zero-authority path: definite local rejection before wire.
    // No key, socket, invented ACK, fill, latency or inventory is involved.
    [[nodiscard]] bool observe_unsent(const NativeOrderCommand& command,
                                     std::int64_t now_monotonic_ns) noexcept;
    [[nodiscard]] bool matches_pending_command(const NativeOrderCommand& command) const noexcept {
        return healthy_ && authority_.matches_pending_command(command);
    }
    [[nodiscard]] bool healthy() const noexcept { return healthy_; }
    [[nodiscard]] std::uint64_t observed_unsent() const noexcept { return observed_unsent_; }
    [[nodiscard]] std::uint64_t lifecycle_events() const noexcept { return lifecycle_events_; }
    [[nodiscard]] std::uint64_t invalid_commands() const noexcept { return invalid_commands_; }

private:
    NativeSettlementAuthority& authority_;
    OmsOrderRecord terminal_record_{};
    std::uint64_t observed_unsent_ = 0;
    std::uint64_t lifecycle_events_ = 0;
    std::uint64_t invalid_commands_ = 0;
    bool healthy_ = true;
};

} // namespace pm::v7
