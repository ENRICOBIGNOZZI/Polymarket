#pragma once

#include <cstdint>
#include <memory>
#include <string_view>
#include <type_traits>

namespace pm::v7 {

enum LatencyTraceField : std::uint32_t {
    TraceFrameReceive = 1U << 0,
    TraceDecodeComplete = 1U << 1,
    TraceArbDecision = 1U << 2,
    TraceRiskAdmitted = 1U << 3,
    TraceSignStart = 1U << 4,
    TraceSignDone = 1U << 5,
    TraceWireStart = 1U << 6,
    TraceWireComplete = 1U << 7,
    TraceHttpAck = 1U << 8,
    TraceUserWsMatch = 1U << 9,
};

struct LatencyTraceFileHeader {
    std::uint64_t magic = 0x3154434152543756ULL; // "V7TRACT1" little-endian marker.
    std::uint32_t version = 1;
    std::uint32_t record_size = 0;
};

struct LatencyTraceRecord {
    std::uint32_t version = 1;
    std::uint32_t valid_mask = 0;
    std::uint64_t trace_id = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t client_order_id = 0;
    std::int64_t frame_receive_monotonic_ns = 0;
    std::int64_t decode_complete_monotonic_ns = 0;
    std::int64_t arb_decision_monotonic_ns = 0;
    std::int64_t risk_admitted_monotonic_ns = 0;
    std::int64_t sign_start_monotonic_ns = 0;
    std::int64_t sign_done_monotonic_ns = 0;
    std::int64_t wire_start_monotonic_ns = 0;
    std::int64_t wire_complete_monotonic_ns = 0;
    std::int64_t http_ack_monotonic_ns = 0;
    std::int64_t user_ws_match_monotonic_ns = 0;
};

struct LatencyTraceWriterSnapshot {
    std::uint64_t published = 0;
    std::uint64_t written = 0;
    std::uint64_t dropped = 0;
    std::uint64_t queued = 0;
    std::uint8_t healthy = 0;
};

class NativeLatencyTraceWriter final {
public:
    explicit NativeLatencyTraceWriter(std::string_view path);
    ~NativeLatencyTraceWriter();
    NativeLatencyTraceWriter(const NativeLatencyTraceWriter&) = delete;
    NativeLatencyTraceWriter& operator=(const NativeLatencyTraceWriter&) = delete;

    [[nodiscard]] bool valid() const noexcept;
    [[nodiscard]] bool publish(const LatencyTraceRecord& record) noexcept;
    void stop() noexcept;
    [[nodiscard]] LatencyTraceWriterSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

static_assert(std::is_trivially_copyable_v<LatencyTraceFileHeader>);
static_assert(std::is_trivially_copyable_v<LatencyTraceRecord>);
static_assert(std::is_standard_layout_v<LatencyTraceRecord>);

} // namespace pm::v7
