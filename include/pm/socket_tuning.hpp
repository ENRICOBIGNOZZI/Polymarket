#pragma once

#include <cerrno>

#if defined(__linux__)
#include <sys/socket.h>
#endif

namespace pm::network {

inline constexpr int kMaxBusyPollUs = 2'000;

[[nodiscard]] inline int apply_busy_poll(
    int fd, int requested_us) noexcept {
    if (requested_us < 0 || requested_us > kMaxBusyPollUs) return EINVAL;
    if (requested_us == 0) return 0;
#if defined(__linux__) && defined(SO_BUSY_POLL)
    if (::setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL,
                     &requested_us, sizeof(requested_us)) != 0) {
        return errno;
    }
    int actual = 0;
    socklen_t size = sizeof(actual);
    if (::getsockopt(fd, SOL_SOCKET, SO_BUSY_POLL,
                     &actual, &size) != 0) {
        return errno;
    }
    return actual == requested_us ? 0 : EIO;
#else
    (void)fd;
    return ENOTSUP;
#endif
}

[[nodiscard]] inline int incoming_cpu(int fd) noexcept {
#if defined(__linux__) && defined(SO_INCOMING_CPU)
    int value = -1;
    socklen_t size = sizeof(value);
    return ::getsockopt(fd, SOL_SOCKET, SO_INCOMING_CPU,
                        &value, &size) == 0 ? value : -1;
#else
    (void)fd;
    return -1;
#endif
}

[[nodiscard]] inline int incoming_napi_id(int fd) noexcept {
#if defined(__linux__) && defined(SO_INCOMING_NAPI_ID)
    int value = -1;
    socklen_t size = sizeof(value);
    return ::getsockopt(fd, SOL_SOCKET, SO_INCOMING_NAPI_ID,
                        &value, &size) == 0 ? value : -1;
#else
    (void)fd;
    return -1;
#endif
}

} // namespace pm::network
