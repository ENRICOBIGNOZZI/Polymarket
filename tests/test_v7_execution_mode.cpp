#include "pm/v7_execution_mode.hpp"

#include <cassert>

int main() {
    using pm::v7::ExecutionMode;
    using pm::v7::execution_capabilities;
    using pm::v7::execution_mode_name;
    using pm::v7::legacy_execution_flags_match;
    using pm::v7::parse_execution_mode;

    const auto paper = parse_execution_mode("PAPER_SIMULATED");
    assert(paper && *paper == ExecutionMode::PaperSimulated);
    assert(execution_mode_name(*paper) == "PAPER_SIMULATED");
    const auto paper_capabilities = execution_capabilities(*paper);
    assert(paper_capabilities.public_ingress && paper_capabilities.paper_orders);
    assert(!paper_capabilities.authenticated_read && !paper_capabilities.signing);
    assert(!paper_capabilities.submit_orders && !paper_capabilities.cancel_orders);
    assert(legacy_execution_flags_match(*paper, true, false, false));
    assert(!legacy_execution_flags_match(*paper, false, false, false));

    const auto shadow = parse_execution_mode("SHADOW_LIVE_READ_ONLY");
    assert(shadow && execution_capabilities(*shadow).authenticated_read);
    assert(!execution_capabilities(*shadow).signing);
    assert(legacy_execution_flags_match(*shadow, false, true, false));

    const auto cancel = parse_execution_mode("CANCEL_ONLY");
    assert(cancel && execution_capabilities(*cancel).cancel_orders);
    assert(!execution_capabilities(*cancel).submit_orders);
    assert(!parse_execution_mode("PAPER_OR_LIVE"));
    return 0;
}
