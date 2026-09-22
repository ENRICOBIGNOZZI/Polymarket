#pragma once

#include "pm/v7_clob_tls.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string_view>

namespace pm::v7::clob {

struct PairedTlsConnectResult {
    std::array<TlsConnectResult, 2> legs{};
    std::uint8_t ready = 0;
};

// Two physically independent persistent HTTP/1.1/TLS sessions.
// Each lane is single-owner. No reconnect, DNS, filesystem or allocation is
// permitted in a caller's reaction path.
class PairedPersistentTlsTransport final {
public:
    PairedPersistentTlsTransport(std::string_view host,
                                 std::uint16_t port = 443,
                                 int timeout_ms = 2'000) noexcept
        : legs_{
            std::make_unique<PersistentTlsSession>(host, port, timeout_ms),
            std::make_unique<PersistentTlsSession>(host, port, timeout_ms)} {}

    [[nodiscard]] PairedTlsConnectResult connect(
        std::string_view ca_file = {}) noexcept {
        PairedTlsConnectResult out{};
        for (std::size_t i = 0; i < legs_.size(); ++i) {
            out.legs[i] = legs_[i]->connect(ca_file);
            if (!out.legs[i].connected) {
                close();
                return out;
            }
        }
        out.ready = 1;
        return out;
    }

    void close() noexcept {
        for (auto& lane : legs_) lane->close();
    }

    [[nodiscard]] bool ready() const noexcept {
        return legs_[0]->connected() && legs_[1]->connected();
    }

    [[nodiscard]] PersistentTlsSession& leg(std::size_t index) noexcept {
        return *legs_[index & 1U];
    }
    [[nodiscard]] const PersistentTlsSession& leg(
        std::size_t index) const noexcept {
        return *legs_[index & 1U];
    }

private:
    std::array<std::unique_ptr<PersistentTlsSession>, 2> legs_{};
};

} // namespace pm::v7::clob
