#pragma once
#include "pm/v7_clob_tls.hpp"
#include <cstdint>
#include <string_view>

namespace pm::v7::clob {
enum class TransportLane : std::uint8_t { Order = 0, Cancel = 1 };
struct DualLaneConnectResult {
    TlsConnectResult order{};
    TlsConnectResult cancel{};
    std::uint8_t ready = 0;
};

// Cold-start owner for two physically independent persistent TLS sessions.
// Each lane remains single-owner; run order and cancel on separate execution
// workers if cancellation must remain independent of an order response stall.
class DualPersistentTlsTransport final {
public:
    DualPersistentTlsTransport(std::string_view host,
                               std::uint16_t port = 443,
                               int timeout_ms = 2'000) noexcept
        : order_(host, port, timeout_ms), cancel_(host, port, timeout_ms) {}

    [[nodiscard]] DualLaneConnectResult connect(std::string_view ca_file = {}) noexcept {
        DualLaneConnectResult out;
        out.order = order_.connect(ca_file);
        if (!out.order.connected) { close(); return out; }
        out.cancel = cancel_.connect(ca_file);
        if (!out.cancel.connected) { close(); return out; }
        out.ready = 1;
        return out;
    }

    void close() noexcept { order_.close(); cancel_.close(); }
    [[nodiscard]] bool ready() const noexcept {
        return order_.connected() && cancel_.connected();
    }
    [[nodiscard]] PersistentTlsSession& lane(TransportLane lane) noexcept {
        return lane == TransportLane::Cancel ? cancel_ : order_;
    }
    [[nodiscard]] const PersistentTlsSession& lane(TransportLane lane) const noexcept {
        return lane == TransportLane::Cancel ? cancel_ : order_;
    }
private:
    PersistentTlsSession order_;
    PersistentTlsSession cancel_;
};
} // namespace pm::v7::clob
