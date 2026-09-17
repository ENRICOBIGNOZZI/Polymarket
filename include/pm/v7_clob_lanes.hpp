#pragma once

#include "pm/v7_clob_tls.hpp"

#include <cstdint>
#include <string_view>

namespace pm::v7::clob {

struct HotLaneConnectResult {
    TlsConnectResult order{};
    TlsConnectResult cancel{};
    std::uint8_t ready = 0;
};

// Two physically independent warm TLS connections to prevent client-side
// head-of-line blocking of risk-driven cancels behind placement traffic.
class HotConnectionLanes final {
public:
    explicit HotConnectionLanes(std::string_view host,
                                std::uint16_t port = 443,
                                int timeout_ms = 2'000) noexcept;

    [[nodiscard]] HotLaneConnectResult prewarm(std::string_view ca_file = {}) noexcept;
    void close() noexcept;
    [[nodiscard]] bool ready() const noexcept;

    [[nodiscard]] PersistentTlsSession& order() noexcept { return order_; }
    [[nodiscard]] PersistentTlsSession& cancel() noexcept { return cancel_; }

private:
    PersistentTlsSession order_;
    PersistentTlsSession cancel_;
};

} // namespace pm::v7::clob
