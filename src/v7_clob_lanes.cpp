#include "pm/v7_clob_lanes.hpp"

namespace pm::v7::clob {

HotConnectionLanes::HotConnectionLanes(std::string_view host,
                                       std::uint16_t port,
                                       int timeout_ms) noexcept
    : order_(host, port, timeout_ms), cancel_(host, port, timeout_ms) {}

HotLaneConnectResult HotConnectionLanes::prewarm(std::string_view ca_file) noexcept {
    HotLaneConnectResult out;
    out.order = order_.connect(ca_file);
    if (!out.order.connected) {
        close();
        return out;
    }
    out.cancel = cancel_.connect(ca_file);
    if (!out.cancel.connected) {
        close();
        return out;
    }
    out.ready = 1;
    return out;
}

void HotConnectionLanes::close() noexcept {
    cancel_.close();
    order_.close();
}

bool HotConnectionLanes::ready() const noexcept {
    return order_.connected() && cancel_.connected();
}

} // namespace pm::v7::clob
