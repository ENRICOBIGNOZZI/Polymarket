#pragma once

#include <atomic>
#include <bit>
#include <cstdint>
#include <memory>
#include <string_view>
#include <type_traits>

namespace pm::v7 {

enum class NativeLatencyStage : std::uint8_t {
    FrameReceive = 1,
    DecodeDone = 2,
    ArbDecision = 3,
    RiskAdmitted = 4,
    SignStart = 5,
    SignDone = 6,
    WireStart = 7,
    WireComplete = 8,
    HttpAck = 9,
    UserWsMatch = 10,
};

struct NativeLatencyEvent {
    std::uint64_t trace_id = 0;
    std::uint64_t client_order_id = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::int64_t timestamp_ns = 0;
    NativeLatencyStage stage = NativeLatencyStage::FrameReceive;
    std::uint8_t reserved[7]{};
};

struct NativeLatencyTapeSnapshot {
    std::uint64_t published = 0;
    std::uint64_t written = 0;
    std::uint64_t dropped = 0;
    std::uint64_t queued = 0;
    std::uint8_t healthy = 0;
};

// Multi-producer / single-writer binary telemetry plane. publish() performs
// only a bounded lock-free copy into preallocated storage. Filesystem I/O is
// owned by the background writer thread and never occurs on reaction threads.
class NativeLatencyTape final {
public:
    explicit NativeLatencyTape(std::string_view path);
    ~NativeLatencyTape();

    NativeLatencyTape(const NativeLatencyTape&) = delete;
    NativeLatencyTape& operator=(const NativeLatencyTape&) = delete;

    [[nodiscard]] bool publish(const NativeLatencyEvent& event) noexcept;
    void stop() noexcept;
    [[nodiscard]] NativeLatencyTapeSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

static_assert(std::endian::native == std::endian::little,
              "native latency tape format is explicitly little-endian");
static_assert(std::is_trivially_copyable_v<NativeLatencyEvent>);
static_assert(std::is_standard_layout_v<NativeLatencyEvent>);
static_assert(sizeof(NativeLatencyEvent) == 48);

} // namespace pm::v7
