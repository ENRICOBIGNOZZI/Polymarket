#pragma once

#include "pm/v7_clob_http1_response.hpp"
#include "pm/v7_clob_tls.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string_view>
#include <thread>

namespace pm::v7::clob {

struct PairTransportLegResult {
    std::size_t request_bytes = 0;
    std::int64_t write_start_monotonic_ns = 0;
    std::int64_t wire_complete_monotonic_ns = 0;
    std::int64_t ack_complete_monotonic_ns = 0;
    int http_status = 0;
    int incoming_cpu = -1;
    int incoming_napi_id = -1;
    TlsTransportError transport_error = TlsTransportError::None;
    std::uint8_t wire_ok = 0;
    std::uint8_t response_ok = 0;
};

struct PairTransportResult {
    PairTransportLegResult yes{};
    PairTransportLegResult no{};
    std::int64_t first_leg_wire_monotonic_ns = 0;
    std::int64_t second_leg_wire_monotonic_ns = 0;
    std::int64_t wire_skew_ns = 0;
    std::int64_t first_ack_monotonic_ns = 0;
    std::int64_t second_ack_monotonic_ns = 0;
    std::int64_t ack_skew_ns = 0;
    std::uint8_t both_wire_ok = 0;
    std::uint8_t both_response_ok = 0;
};

struct BatchTransportResult {
    std::size_t request_bytes = 0;
    std::int64_t write_start_monotonic_ns = 0;
    std::int64_t wire_complete_monotonic_ns = 0;
    std::int64_t ack_complete_monotonic_ns = 0;
    int http_status = 0;
    int incoming_cpu = -1;
    int incoming_napi_id = -1;
    TlsTransportError transport_error = TlsTransportError::None;
    std::uint8_t wire_ok = 0;
    std::uint8_t response_ok = 0;
};

struct PairTransportConnectResult {
    TlsConnectResult yes{};
    TlsConnectResult no{};
    TlsConnectResult batch{};
    std::uint8_t ready = 0;
};

// Transport-only layer for already-built HTTP/1.1 frames.
//
// YES and NO each have one dedicated persistent TLS session and one persistent
// worker. The caller remains the sole risk/OMS owner; these workers only write
// bytes and parse responses. The third connection is used exclusively for the
// POST /orders comparison arm. A successful batch response is NOT treated as
// atomic multi-leg execution: the venue may accept/reject individual orders.
class PairPersistentTlsTransport final {
public:
    PairPersistentTlsTransport(std::string_view host,
                               std::uint16_t port = 443,
                               int timeout_ms = 2'000,
                               int socket_busy_poll_us = 0) noexcept;
    ~PairPersistentTlsTransport();

    PairPersistentTlsTransport(const PairPersistentTlsTransport&) = delete;
    PairPersistentTlsTransport& operator=(const PairPersistentTlsTransport&) = delete;

    [[nodiscard]] PairTransportConnectResult connect(
        std::string_view ca_file = {}) noexcept;
    void close() noexcept;
    [[nodiscard]] bool ready() const noexcept;

    // Single-caller API. Frame memory must remain valid until this call returns.
    [[nodiscard]] PairTransportResult submit_parallel(
        std::span<const char> yes_frame,
        std::span<const char> no_frame) noexcept;

    // Comparison arm only. No atomicity semantics are inferred from this result.
    [[nodiscard]] BatchTransportResult submit_batch(
        std::span<const char> batch_frame) noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::v7::clob
