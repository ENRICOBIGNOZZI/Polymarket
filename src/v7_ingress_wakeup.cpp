#include "pm/v7_ingress_wakeup.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <climits>
#include <fcntl.h>
#include <poll.h>
#include <system_error>
#include <thread>
#include <unistd.h>
#if defined(__linux__)
#include <sys/eventfd.h>
#endif

namespace pm::v7::external_fair {
namespace {

inline void cpu_relax() noexcept {
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#elif defined(__aarch64__) || defined(__arm__)
    __asm__ __volatile__("yield" ::: "memory");
#else
    std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

} // namespace

IngressWakeup::IngressWakeup() {
#if defined(__linux__)
    read_fd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (read_fd_ < 0) throw std::system_error(errno, std::generic_category(), "eventfd");
    write_fd_ = read_fd_;
#else
    int descriptors[2];
    if (::pipe(descriptors) != 0) throw std::system_error(errno, std::generic_category(), "pipe");
    for (const int fd : descriptors) {
        if (::fcntl(fd, F_SETFL, O_NONBLOCK) < 0 || ::fcntl(fd, F_SETFD, FD_CLOEXEC) < 0) {
            const int error = errno;
            ::close(descriptors[0]);
            ::close(descriptors[1]);
            throw std::system_error(error, std::generic_category(), "nonblocking wakeup pipe");
        }
    }
    read_fd_ = descriptors[0];
    write_fd_ = descriptors[1];
#endif
}

IngressWakeup::~IngressWakeup() {
    if (read_fd_ >= 0) ::close(read_fd_);
    if (write_fd_ >= 0 && write_fd_ != read_fd_) ::close(write_fd_);
}

bool IngressWakeup::consume_generation() noexcept {
    const auto current = generation_.load(std::memory_order_acquire);
    if (current == observed_generation_) return false;
    observed_generation_ = current;
    return true;
}

void IngressWakeup::signal_kernel_waiter() noexcept {
#if defined(__linux__)
    const std::uint64_t signal = 1;
#else
    const unsigned char signal = 1;
#endif
    ssize_t result;
    do { result = ::write(write_fd_, &signal, sizeof(signal)); }
    while (result < 0 && errno == EINTR);
    if (result >= 0) {
        kernel_wakeups_.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    // Saturation means a notification is already pending. It never means
    // dropping a market event: the bounded ingress queues still own it.
    if (errno != EAGAIN && errno != EWOULDBLOCK) {
        errors_.fetch_add(1, std::memory_order_relaxed);
    }
}

void IngressWakeup::drain_kernel_signal() noexcept {
    for (;;) {
#if defined(__linux__)
        std::uint64_t signals = 0;
#else
        unsigned char signals[4096];
#endif
        ssize_t consumed;
        do { consumed = ::read(read_fd_, &signals, sizeof(signals)); }
        while (consumed < 0 && errno == EINTR);
        if (consumed > 0) {
#if defined(__linux__)
            return; // one eventfd read drains the accumulated counter
#else
            continue; // drain every byte written while the pipe was armed
#endif
        }
        if (consumed < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;
        if (consumed == 0) return;
        errors_.fetch_add(1, std::memory_order_relaxed);
        return;
    }
}

void IngressWakeup::notify() noexcept {
    // Publish the queue mutation before making the generation visible.
    generation_.fetch_add(1, std::memory_order_release);

    // Fast case: the consumer is active or inside its userspace spin window.
    // No write(), eventfd wakeup or scheduler transition is required.
    if (!sleeping_.load(std::memory_order_acquire)) return;

    // Slow case only: the consumer armed a blocking poll. A redundant write is
    // harmless because the generation counter is the source of truth.
    signal_kernel_waiter();
}

bool IngressWakeup::wait_for(
    std::chrono::milliseconds timeout,
    std::chrono::microseconds spin_budget) noexcept {
    timeout = std::clamp(timeout, std::chrono::milliseconds::zero(),
                         std::chrono::milliseconds(INT_MAX));
    spin_budget = std::max(spin_budget, std::chrono::microseconds::zero());

    // Consume notifications published before wait_for() without touching the
    // kernel. This also coalesces bursts exactly as the old eventfd path did.
    if (consume_generation()) {
        drain_kernel_signal();
        return true;
    }
    if (timeout == std::chrono::milliseconds::zero()) return false;

    const auto deadline = std::chrono::steady_clock::now() + timeout;
    const auto spin_deadline = std::min(
        deadline, std::chrono::steady_clock::now()
            + std::chrono::duration_cast<std::chrono::steady_clock::duration>(spin_budget));
    while (std::chrono::steady_clock::now() < spin_deadline) {
        if (consume_generation()) {
            drain_kernel_signal();
            return true;
        }
        cpu_relax();
    }

    // Arm blocking sleep, then recheck generation. A producer racing this arm
    // either observes sleeping_=false and is caught by this recheck, or sees
    // true and also signals the kernel fd. There is no lost-wakeup interval.
    sleeping_.store(true, std::memory_order_release);
    if (consume_generation()) {
        sleeping_.store(false, std::memory_order_release);
        drain_kernel_signal();
        return true;
    }

    pollfd descriptor{read_fd_, POLLIN, 0};
    for (;;) {
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) {
            sleeping_.store(false, std::memory_order_release);
            if (consume_generation()) {
                drain_kernel_signal();
                return true;
            }
            return false;
        }
        const auto remaining = std::chrono::ceil<std::chrono::milliseconds>(deadline - now);
        const int remaining_ms = static_cast<int>(std::clamp<std::int64_t>(
            remaining.count(), 0, INT_MAX));
        descriptor.revents = 0;
        const int ready = ::poll(&descriptor, 1, remaining_ms);
        if (ready < 0 && errno == EINTR) continue;

        sleeping_.store(false, std::memory_order_release);
        if (ready < 0 || (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
            errors_.fetch_add(1, std::memory_order_relaxed);
            if (consume_generation()) {
                drain_kernel_signal();
                return true;
            }
            std::this_thread::sleep_until(deadline);
            return false;
        }
        if (ready > 0 && (descriptor.revents & POLLIN) != 0) drain_kernel_signal();
        // Generation is authoritative. It handles both a normal fd wake and a
        // producer that raced poll timeout / sleeping_=false without writing.
        if (consume_generation()) return true;
        if (ready == 0) return false;
        // A stale coalesced fd byte can exist only from a prior armed sleep.
        // If it carried no new generation, re-arm and continue until deadline.
        sleeping_.store(true, std::memory_order_release);
        if (consume_generation()) {
            sleeping_.store(false, std::memory_order_release);
            drain_kernel_signal();
            return true;
        }
    }
}

} // namespace pm::v7::external_fair
