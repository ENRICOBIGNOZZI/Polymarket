#include "pm/v7_execution_mode.hpp"

namespace pm::v7 {

std::optional<ExecutionMode> parse_execution_mode(const std::string_view value) noexcept {
    if (value == "RESEARCH_ZERO_AUTHORITY") return ExecutionMode::ResearchZeroAuthority;
    if (value == "PAPER_SIMULATED") return ExecutionMode::PaperSimulated;
    if (value == "SHADOW_LIVE_READ_ONLY") return ExecutionMode::ShadowLiveReadOnly;
    if (value == "MICRO_LIVE") return ExecutionMode::MicroLive;
    if (value == "LIVE_RESTRICTED") return ExecutionMode::LiveRestricted;
    if (value == "LIVE_SCALED") return ExecutionMode::LiveScaled;
    if (value == "DRAIN_ONLY") return ExecutionMode::DrainOnly;
    if (value == "CANCEL_ONLY") return ExecutionMode::CancelOnly;
    if (value == "KILLED") return ExecutionMode::Killed;
    return std::nullopt;
}

std::string_view execution_mode_name(const ExecutionMode mode) noexcept {
    switch (mode) {
        case ExecutionMode::ResearchZeroAuthority: return "RESEARCH_ZERO_AUTHORITY";
        case ExecutionMode::PaperSimulated: return "PAPER_SIMULATED";
        case ExecutionMode::ShadowLiveReadOnly: return "SHADOW_LIVE_READ_ONLY";
        case ExecutionMode::MicroLive: return "MICRO_LIVE";
        case ExecutionMode::LiveRestricted: return "LIVE_RESTRICTED";
        case ExecutionMode::LiveScaled: return "LIVE_SCALED";
        case ExecutionMode::DrainOnly: return "DRAIN_ONLY";
        case ExecutionMode::CancelOnly: return "CANCEL_ONLY";
        case ExecutionMode::Killed: return "KILLED";
    }
    return "KILLED";
}

ExecutionCapabilities execution_capabilities(const ExecutionMode mode) noexcept {
    switch (mode) {
        case ExecutionMode::ResearchZeroAuthority:
            return {.public_ingress = true};
        case ExecutionMode::PaperSimulated:
            return {.public_ingress = true, .paper_orders = true};
        case ExecutionMode::ShadowLiveReadOnly:
            return {.public_ingress = true, .authenticated_read = true};
        case ExecutionMode::MicroLive:
        case ExecutionMode::LiveRestricted:
        case ExecutionMode::LiveScaled:
            return {.public_ingress = true, .authenticated_read = true, .signing = true,
                    .submit_orders = true, .cancel_orders = true, .authoritative_pnl = true};
        case ExecutionMode::DrainOnly:
        case ExecutionMode::CancelOnly:
            return {.public_ingress = true, .authenticated_read = true, .cancel_orders = true};
        case ExecutionMode::Killed:
            return {};
    }
    return {};
}

bool legacy_execution_flags_match(const ExecutionMode mode, const bool paper_only,
                                  const bool authenticated_execution,
                                  const bool real_order_submission) noexcept {
    const auto capabilities = execution_capabilities(mode);
    return paper_only == capabilities.paper_orders
        && authenticated_execution == capabilities.authenticated_read
        && real_order_submission == capabilities.submit_orders;
}

} // namespace pm::v7
