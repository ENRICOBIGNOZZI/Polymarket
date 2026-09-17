#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::latency {

enum class RxTimestampSource : std::uint8_t {
    None = 0,
    SoftwareSocketTimestamp = 1,
    HardwareNicTimestamp = 2,
    EbpfIngressTimestamp = 3,
};

enum class Stamp : std::uint8_t {
    KernelRx = 0,
    ReadComplete = 1,
    ParseComplete = 2,
    StateApplied = 3,
    SignalReady = 4,
    DecisionComplete = 5,
    RiskAdmitted = 6,
    EncodeComplete = 7,
    SignComplete = 8,
    WireComplete = 9,
    FirstResponseByte = 10,
    ResponseComplete = 11,
    UserWsReceive = 12,
    Count = 13,
};

struct HftLatencyTrace {
    std::uint64_t causal_id = 0;
    std::uint32_t valid_mask = 0;
    RxTimestampSource rx_source = RxTimestampSource::None;
    std::array<std::uint8_t, 3> reserved{};
    std::array<std::int64_t, static_cast<std::size_t>(Stamp::Count)> timestamp_ns{};

    [[nodiscard]] bool record(Stamp stamp, std::int64_t monotonic_ns) noexcept {
        if (monotonic_ns <= 0) return false;
        const auto index = static_cast<std::size_t>(stamp);
        if (index >= timestamp_ns.size()) return false;
        timestamp_ns[index] = monotonic_ns;
        valid_mask |= (1U << index);
        return true;
    }

    [[nodiscard]] bool record_kernel_rx(
        std::int64_t monotonic_ns,
        RxTimestampSource source) noexcept {
        if (source == RxTimestampSource::None) return false;
        if (!record(Stamp::KernelRx, monotonic_ns)) return false;
        rx_source = source;
        return true;
    }

    [[nodiscard]] bool has(Stamp stamp) const noexcept {
        const auto index = static_cast<std::size_t>(stamp);
        return index < timestamp_ns.size() && (valid_mask & (1U << index)) != 0;
    }

    [[nodiscard]] std::int64_t at(Stamp stamp) const noexcept {
        const auto index = static_cast<std::size_t>(stamp);
        return index < timestamp_ns.size() ? timestamp_ns[index] : 0;
    }
};

struct LatencyLeg {
    std::int64_t ns = 0;
    std::uint8_t valid = 0;
};

[[nodiscard]] constexpr LatencyLeg leg(
    const HftLatencyTrace& trace,
    Stamp start,
    Stamp end) noexcept {
    if (!trace.has(start) || !trace.has(end)) return {};
    const auto first = trace.at(start);
    const auto last = trace.at(end);
    if (first <= 0 || last < first) return {};
    return {last - first, 1};
}

struct LatencyDistribution {
    std::uint64_t samples = 0;
    std::int64_t p50_ns = 0;
    std::int64_t p95_ns = 0;
    std::int64_t p99_ns = 0;
    std::int64_t p99_9_ns = 0;
    std::int64_t max_ns = 0;
};

template <std::size_t Capacity>
class FixedLatencySeries final {
    static_assert(Capacity > 0);
public:
    void add(std::int64_t ns) noexcept {
        if (ns < 0) return;
        samples_[next_] = ns;
        next_ = (next_ + 1) % Capacity;
        if (size_ < Capacity) ++size_;
        ++accepted_;
    }

    void add(LatencyLeg value) noexcept {
        if (value.valid != 0) add(value.ns);
    }

    [[nodiscard]] std::size_t retained() const noexcept { return size_; }
    [[nodiscard]] std::uint64_t accepted() const noexcept { return accepted_; }

    // Cold/report path. Hot updates remain O(1), allocation-free and bounded.
    [[nodiscard]] LatencyDistribution distribution() const noexcept {
        LatencyDistribution out;
        out.samples = size_;
        if (size_ == 0) return out;
        std::array<std::int64_t, Capacity> sorted{};
        for (std::size_t i = 0; i < size_; ++i) sorted[i] = samples_[i];
        std::sort(sorted.begin(), sorted.begin() + static_cast<std::ptrdiff_t>(size_));
        const auto quantile = [&](std::uint64_t numerator, std::uint64_t denominator) noexcept {
            const auto index = static_cast<std::size_t>(
                (static_cast<std::uint64_t>(size_ - 1) * numerator) / denominator);
            return sorted[index];
        };
        out.p50_ns = quantile(500, 1000);
        out.p95_ns = quantile(950, 1000);
        out.p99_ns = quantile(990, 1000);
        out.p99_9_ns = quantile(999, 1000);
        out.max_ns = sorted[size_ - 1];
        return out;
    }

private:
    std::array<std::int64_t, Capacity> samples_{};
    std::size_t next_ = 0;
    std::size_t size_ = 0;
    std::uint64_t accepted_ = 0;
};

static_assert(std::is_trivially_copyable_v<HftLatencyTrace>);
static_assert(std::is_standard_layout_v<HftLatencyTrace>);
static_assert(std::is_trivially_copyable_v<LatencyDistribution>);

} // namespace pm::v7::latency
