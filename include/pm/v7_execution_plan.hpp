#pragma once

#include "pm/v7_intent.hpp"

#include <cstdint>
#include <type_traits>

namespace pm::v7 {

enum class ExecutionPolicyId : std::uint8_t {
    PassiveMaker = 1,
    PatientPassive = 2,
    AggressiveTaker = 3,
    Basket = 4,
    MultiLeg = 5,
    Liquidation = 6,
    Emergency = 7,
};

// Common hand-off between strategy/risk and the single execution/OMS owner.
// StrategyIntent remains strategy-neutral. Policy-specific execution context is
// attached here instead of contaminating the strategy contract or forcing slow
// policies through Maker semantics.
struct ExecutionPlan {
    StrategyIntent intent{};
    std::int64_t public_queue_ahead_microunits = 0;
    std::int32_t tick_size_e4 = 0;
    std::uint64_t market_state_version = 0;
    ExecutionPolicyId policy = ExecutionPolicyId::PassiveMaker;
    std::uint8_t public_queue_observable = 0;
    std::uint8_t reserved0 = 0;
    std::uint8_t reserved1 = 0;
    std::uint8_t reserved2 = 0;
};

static_assert(std::is_trivially_copyable_v<ExecutionPlan>);
static_assert(std::is_standard_layout_v<ExecutionPlan>);

[[nodiscard]] inline bool execution_plan_is_critical(
    const ExecutionPlan& plan) noexcept {
    return is_critical_intent(plan.intent.type)
        || plan.intent.urgency == Urgency::Critical
        || plan.policy == ExecutionPolicyId::Emergency;
}

} // namespace pm::v7
