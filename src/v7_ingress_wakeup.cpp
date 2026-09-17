#include "pm/v7_ingress_wakeup.hpp"

#include <algorithm>
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

void IngressWakeup::notify() noexcept {
#if defined(__linux__)
    const std::uint64_t signal = 1;
#else
    const unsigned char signal = 1;
#endif
    ssize_t result;
    do { result = ::write(write_fd_, &signal, sizeof(signal)); }
    while (result < 0 && errno == EINTR);
    // Saturation means a notification is already pending. It never means
    // dropping a market event: the bounded ingress queues still own it.
    if (result < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
        errors_.fetch_add(1, std::memory_order_relaxed);
    }
}

bool IngressWakeup::wait_for(std::chrono::milliseconds timeout) noexcept {
    timeout = std::clamp(timeout, std::chrono::milliseconds::zero(),
                         std::chrono::milliseconds(INT_MAX));
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    int remaining_ms = static_cast<int>(timeout.count());
    pollfd descriptor{read_fd_, POLLIN, 0};
    for (;;) {
        const int ready = ::poll(&descriptor, 1, remaining_ms);
        if (ready == 0) return false;
        if (ready < 0 && errno == EINTR) {
            const auto now = std::chrono::steady_clock::now();
            if (now >= deadline) return false;
            remaining_ms = static_cast<int>(
                std::chrono::ceil<std::chrono::milliseconds>(deadline - now).count());
            continue;
        }
        if (ready < 0 || (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
            errors_.fetch_add(1, std::memory_order_relaxed);
            std::this_thread::sleep_until(deadline);
            return false;
        }
        if ((descriptor.revents & POLLIN) == 0) return false;
#if defined(__linux__)
        std::uint64_t signals = 0;
#else
        unsigned char signals[4096];
#endif
        ssize_t consumed;
        do { consumed = ::read(read_fd_, &signals, sizeof(signals)); }
        while (consumed < 0 && errno == EINTR);
        if (consumed > 0) return true;
        if (consumed < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return false;
        errors_.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
}

} // namespace pm::v7::external_fair
