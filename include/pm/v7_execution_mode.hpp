#pragma once

#include <optional>
#include <string_view>

namespace pm::v7 {

// The only authority modes accepted by V7.  The enum is deliberately small
// and pure: transports and signers consume its capabilities but cannot infer
// authority from an arbitrary combination of legacy booleans.
enum class ExecutionMode : unsigned char {
    ResearchZeroAuthority,
    PaperSimulated,
    ShadowLiveReadOnly,
    MicroLive,
    LiveRestricted,
    LiveScaled,
    DrainOnly,
    CancelOnly,
    Killed,
};

struct ExecutionCapabilities {
    bool public_ingress = false;
    bool paper_orders = false;
    bool authenticated_read = false;
    bool signing = false;
    bool submit_orders = false;
    bool cancel_orders = false;
    bool authoritative_pnl = false;
};

[[nodiscard]] std::optional<ExecutionMode> parse_execution_mode(std::string_view value) noexcept;
[[nodiscard]] std::string_view execution_mode_name(ExecutionMode mode) noexcept;
[[nodiscard]] ExecutionCapabilities execution_capabilities(ExecutionMode mode) noexcept;

// Migration guard for the legacy paper/authenticated/submission triple.  It
// verifies compatibility; it does not let the triple select an ambiguous mode.
[[nodiscard]] bool legacy_execution_flags_match(ExecutionMode mode, bool paper_only,
                                                 bool authenticated_execution,
                                                 bool real_order_submission) noexcept;

} // namespace pm::v7
