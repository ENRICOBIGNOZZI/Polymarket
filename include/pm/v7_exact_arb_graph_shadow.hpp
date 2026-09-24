#pragma once
#include "pm/v7_exact_arb_graph_loader.hpp"
#include "pm/v7_market_ws.hpp"

#include <filesystem>

namespace pm::v7::exact_arb_graph {

struct NativeFrameClock {
    std::int64_t receive_wall_ms = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::int64_t decode_complete_monotonic_ns = 0;
};

// Dedicated zero-authority observer adapter. refresh/drain/status are CONTROL
// only; on_frame is FEED only. Join the feed before destruction. The adapter
// emits pre-allocation mathematical observations, NOT fills or earned PnL.
class NativeGraphShadow final {
public:
    NativeGraphShadow(std::filesystem::path output, std::string model_sha,
                      std::string session, std::vector<NativeTokenBinding> bindings);
    ~NativeGraphShadow();
    NativeGraphShadow(const NativeGraphShadow&) = delete;
    NativeGraphShadow& operator=(const NativeGraphShadow&) = delete;
    void refresh(std::string_view selection, std::string_view capital_policy,
                 std::int64_t wall_ms, std::int64_t monotonic_ns) noexcept;
    void invalidate(std::string reason) noexcept;
    void on_frame(const MarketWsShard& decoder, std::span<const MarketWsEvent> events,
                  const NativeFrameClock& clock, std::uint64_t epoch, bool source_frame_valid,
                  std::string_view raw_frame) noexcept;
    void drain(bool disk_pressure);
    void write_status(std::int64_t wall_ms, bool stopped = false);
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace pm::v7::exact_arb_graph
