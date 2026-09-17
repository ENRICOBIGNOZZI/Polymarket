#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>

namespace pm::v7::external_fair {

// Multi-producer notification, single-consumer wait. The queues, not this
// coalescing signal, own the events. Producers never wait for the consumer.
// Keep this object alive until every producer and consumer has been joined.
class IngressWakeup final {
public:
    IngressWakeup();
    ~IngressWakeup();
    IngressWakeup(const IngressWakeup&) = delete;
    IngressWakeup& operator=(const IngressWakeup&) = delete;

    void notify() noexcept;
    [[nodiscard]] bool wait_for(std::chrono::milliseconds timeout) noexcept;
    [[nodiscard]] std::uint64_t errors() const noexcept {
        return errors_.load(std::memory_order_relaxed);
    }
private:
    int read_fd_ = -1;
    int write_fd_ = -1;
    std::atomic<std::uint64_t> errors_{0};
};

} // namespace pm::v7::external_fair
