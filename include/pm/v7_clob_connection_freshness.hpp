#pragma once

#include <cstdint>
#include <type_traits>

namespace pm::v7::clob {

// Single-owner freshness gate for one persistent authenticated transport lane.
// A TCP/TLS connection is not sufficient for authority: a caller must record a
// complete application-level round trip after connect and keep it fresh.
class ConnectionFreshnessGate final {
public:
    explicit ConnectionFreshnessGate(std::int64_t max_idle_ns) noexcept
        : max_idle_ns_(max_idle_ns), valid_(max_idle_ns > 0) {}

    [[nodiscard]] bool valid() const noexcept { return valid_; }

    // Call after a new TCP/TLS session is established. The lane remains cold
    // until record_roundtrip() confirms that application traffic completed.
    void on_connect(std::int64_t now_ns) noexcept {
        if (!valid_ || now_ns <= 0) { connected_ = false; return; }
        connected_ = true;
        connected_ns_ = now_ns;
        last_roundtrip_ns_ = 0;
        ++epoch_;
        if (epoch_ == 0) ++epoch_;
    }

    void on_disconnect() noexcept {
        connected_ = false;
        connected_ns_ = 0;
        last_roundtrip_ns_ = 0;
    }

    [[nodiscard]] bool record_roundtrip(std::int64_t completed_ns) noexcept {
        if (!valid_ || !connected_ || completed_ns < connected_ns_
            || (last_roundtrip_ns_ != 0 && completed_ns < last_roundtrip_ns_)) return false;
        last_roundtrip_ns_ = completed_ns;
        return true;
    }

    [[nodiscard]] bool ready(std::int64_t now_ns) const noexcept {
        if (!valid_ || !connected_ || last_roundtrip_ns_ == 0 || now_ns < last_roundtrip_ns_) return false;
        return now_ns - last_roundtrip_ns_ <= max_idle_ns_;
    }

    [[nodiscard]] bool heartbeat_due(std::int64_t now_ns,
                                     std::int64_t lead_ns) const noexcept {
        if (!valid_ || !connected_ || lead_ns < 0 || lead_ns >= max_idle_ns_ || now_ns <= 0) return false;
        if (last_roundtrip_ns_ == 0) return true;
        if (now_ns < last_roundtrip_ns_) return true;
        return now_ns - last_roundtrip_ns_ >= max_idle_ns_ - lead_ns;
    }

    [[nodiscard]] std::uint64_t epoch() const noexcept { return epoch_; }
    [[nodiscard]] std::int64_t last_roundtrip_ns() const noexcept { return last_roundtrip_ns_; }

private:
    std::int64_t max_idle_ns_ = 0;
    std::int64_t connected_ns_ = 0;
    std::int64_t last_roundtrip_ns_ = 0;
    std::uint64_t epoch_ = 0;
    bool valid_ = false;
    bool connected_ = false;
};

static_assert(std::is_trivially_copyable_v<ConnectionFreshnessGate>);

} // namespace pm::v7::clob
