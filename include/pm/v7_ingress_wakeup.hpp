#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>

namespace pm::v7::external_fair {

// Multi-producer notification, single-consumer wait. The queues, not this
// coalescing signal, own the events. Producers never wait for the consumer.
//
// The hot path is generation based. While the consumer is running/spinning,
// notify() is syscall-free: producers publish one release increment and return.
// eventfd/pipe is touched only when the consumer has explicitly armed sleep.
// The arm -> generation recheck closes the classic lost-wakeup race.
class IngressWakeup final {
public:
    IngressWakeup();
    ~IngressWakeup();
    IngressWakeup(const IngressWakeup&) = delete;
    IngressWakeup& operator=(const IngressWakeup&) = delete;

    void notify() noexcept;
    [[nodiscard]] bool wait_for(
        std::chrono::milliseconds timeout,
        std::chrono::microseconds spin_budget = std::chrono::microseconds::zero()) noexcept;

    [[nodiscard]] std::uint64_t errors() const noexcept {
        return errors_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] std::uint64_t kernel_wakeups() const noexcept {
        return kernel_wakeups_.load(std::memory_order_relaxed);
    }
private:
    void signal_kernel_waiter() noexcept;
    void drain_kernel_signal() noexcept;
    [[nodiscard]] bool consume_generation() noexcept;

    int read_fd_ = -1;
    int write_fd_ = -1;
    std::atomic<std::uint64_t> generation_{0};
    std::atomic<bool> sleeping_{false};
    // Single-consumer state. It is deliberately non-atomic.
    std::uint64_t observed_generation_ = 0;
    std::atomic<std::uint64_t> kernel_wakeups_{0};
    std::atomic<std::uint64_t> errors_{0};
};

} // namespace pm::v7::external_fair
