#pragma once

#include "pm/v7_clob_tls.hpp"

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string_view>

namespace pm::v7::clob {

template <std::size_t OrderLanes>
struct MultiOrderConnectResult {
    std::array<TlsConnectResult, OrderLanes> orders{};
    TlsConnectResult cancel{};
    std::uint8_t ready = 0;
};

// Cold-start owner for independent persistent HTTP/1.1/TLS order lanes plus
// one physically separate cancel lane. Economic/risk authority and rate limits
// remain upstream: each session must still have exactly one transport worker.
template <std::size_t OrderLanes = 4>
class MultiOrderPersistentTlsTransport final {
    static_assert(OrderLanes >= 2 && OrderLanes <= 16);
    static_assert(std::has_single_bit(OrderLanes),
                  "order lane count must be a power of two");

public:
    MultiOrderPersistentTlsTransport(std::string_view host,
                                     std::uint16_t port = 443,
                                     int timeout_ms = 2'000) noexcept {
        for (auto& lane : orders_) {
            lane = std::make_unique<PersistentTlsSession>(host, port, timeout_ms);
        }
        cancel_ = std::make_unique<PersistentTlsSession>(host, port, timeout_ms);
    }

    [[nodiscard]] MultiOrderConnectResult<OrderLanes> connect(
        std::string_view ca_file = {}) noexcept {
        MultiOrderConnectResult<OrderLanes> out;
        for (std::size_t index = 0; index < OrderLanes; ++index) {
            out.orders[index] = orders_[index]->connect(ca_file);
            if (!out.orders[index].connected) {
                close();
                return out;
            }
        }
        out.cancel = cancel_->connect(ca_file);
        if (!out.cancel.connected) {
            close();
            return out;
        }
        out.ready = 1;
        return out;
    }

    void close() noexcept {
        for (auto& lane : orders_) lane->close();
        cancel_->close();
    }

    [[nodiscard]] bool ready() const noexcept {
        if (!cancel_->connected()) return false;
        for (const auto& lane : orders_) {
            if (!lane->connected()) return false;
        }
        return true;
    }

    [[nodiscard]] constexpr std::size_t order_lane_count() const noexcept {
        return OrderLanes;
    }

    [[nodiscard]] PersistentTlsSession& order_lane(std::size_t index) noexcept {
        return *orders_[index & (OrderLanes - 1U)];
    }

    [[nodiscard]] const PersistentTlsSession& order_lane(
        std::size_t index) const noexcept {
        return *orders_[index & (OrderLanes - 1U)];
    }

    // Deterministic single-owner routing. The execution owner chooses the key
    // (normally market/instrument handle), then hands the request to that lane's
    // sole worker. No two workers may write one TLS session concurrently.
    [[nodiscard]] PersistentTlsSession& order_lane_for(
        std::uint64_t routing_key) noexcept {
        return order_lane(static_cast<std::size_t>(routing_key));
    }

    // Hot failover only across sessions that were already prewarmed. Never
    // reconnect here: network recovery stays off the causal submit path.
    [[nodiscard]] PersistentTlsSession* try_connected_order_lane_for(
        std::uint64_t routing_key) noexcept {
        const std::size_t preferred = static_cast<std::size_t>(routing_key) & (OrderLanes - 1U);
        for (std::size_t step = 0; step < OrderLanes; ++step) {
            auto& candidate = *orders_[(preferred + step) & (OrderLanes - 1U)];
            if (candidate.connected()) return &candidate;
        }
        return nullptr;
    }

    [[nodiscard]] const PersistentTlsSession* try_connected_order_lane_for(
        std::uint64_t routing_key) const noexcept {
        const std::size_t preferred = static_cast<std::size_t>(routing_key) & (OrderLanes - 1U);
        for (std::size_t step = 0; step < OrderLanes; ++step) {
            const auto& candidate = *orders_[(preferred + step) & (OrderLanes - 1U)];
            if (candidate.connected()) return &candidate;
        }
        return nullptr;
    }

    [[nodiscard]] PersistentTlsSession& cancel_lane() noexcept { return *cancel_; }
    [[nodiscard]] const PersistentTlsSession& cancel_lane() const noexcept { return *cancel_; }

private:
    std::array<std::unique_ptr<PersistentTlsSession>, OrderLanes> orders_{};
    std::unique_ptr<PersistentTlsSession> cancel_{};
};

} // namespace pm::v7::clob
