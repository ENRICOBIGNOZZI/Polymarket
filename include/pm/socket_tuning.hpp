#pragma once

#include <cerrno>

#if defined(__linux__)
#include <sys/socket.h>
#endif

namespace pm::network {

inline constexpr int kMaxBusyPollUs = 2000;

// Cold-path socket setup. A requested tuning must either be applied exactly
// or fail observably; there is no silent latency-mode downgrade.
inline int apply_busy_poll(int fd, int requested_us) noexcept {
    if (requested_us < 0 || requested_us > kMaxBusyPollUs) return EINVAL;
    if (requested_us == 0) return 0;
#if defined(__linux__) && defined(SO_BUSY_POLL)
    if (::setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL,
                     &requested_us, sizeof(requested_us)) != 0) return errno;
    int actual = 0;
    socklen_t length = sizeof(actual);
    if (::getsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &actual, &length) != 0) return errno;
    return actual == requested_us ? 0 : EIO;
#else
    (void)fd;
    return ENOTSUP;
#endif
}

} // namespace pm::network
