#pragma once

#include "pm/v7_clob_order_identity.hpp"
#include "pm/v7_oms.hpp"
#include "pm/v7_user_ws.hpp"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string_view>

namespace pm::v7 {

struct RoutedOmsEvent {
    std::uint64_t client_order_id = 0;
    OmsEvent event{};
};

struct UserOmsBridgeResult {
    std::size_t output_count = 0;
    std::uint8_t invalid_ack = 0;
    std::uint8_t identity_conflict = 0;
    std::uint8_t output_overflow = 0;
    std::uint8_t pending_overflow = 0;
    std::uint8_t duplicate_fill = 0;
};

struct UserOmsBridgeSnapshot {
    std::size_t identities = 0;
    std::size_t pending_fills = 0;
    std::size_t pending_lifecycle = 0;
    std::uint64_t routed_fills = 0;
    std::uint64_t duplicate_fills = 0;
    std::uint64_t foreign_correlations = 0;
    std::uint64_t pending_overflow = 0;
    std::uint64_t dedupe_bucket_overflow = 0;
};

// Single-owner authority bridge. HTTP POST /order ACK establishes the exact
// exchange-order-id mapping. User-WS lifecycle/fill events may route to OMS only
// through that byte-exact mapping. An early user event is retained in bounded
// pending storage until the ACK arrives; it is never guessed or discarded.
class UserOmsBridge final {
public:
    UserOmsBridge();
    ~UserOmsBridge();
    UserOmsBridge(const UserOmsBridge&) = delete;
    UserOmsBridge& operator=(const UserOmsBridge&) = delete;

    [[nodiscard]] UserOmsBridgeResult on_post_order_ack(
        std::uint64_t client_order_id,
        std::string_view response_body,
        std::int64_t response_complete_monotonic_ns,
        std::span<RoutedOmsEvent> output) noexcept;

    [[nodiscard]] UserOmsBridgeResult on_user_event(
        const user_ws::Event& event,
        std::span<RoutedOmsEvent> output) noexcept;

    // Call after the canonical OMS reaches a terminal state and no further
    // exchange updates for this order are expected. This releases exact mapping
    // and recent-fill dedupe storage for long-running bounded operation.
    [[nodiscard]] bool release(std::uint64_t client_order_id) noexcept;

    [[nodiscard]] clob_identity::IdentityLookup lookup_client(
        std::string_view exchange_order_id) const noexcept;
    [[nodiscard]] clob_identity::ExchangeLookup lookup_exchange(
        std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] UserOmsBridgeSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::v7
