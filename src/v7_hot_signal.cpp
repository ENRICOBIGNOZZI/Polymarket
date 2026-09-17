#include "pm/v7_hot_signal.hpp"

#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace pm::v7 {

HotSignalDatagramSender::HotSignalDatagramSender(const std::filesystem::path& destination)
    : destination_(destination) {
    if (destination_.empty() || destination_.string().size() >= sizeof(sockaddr_un::sun_path))
        throw std::invalid_argument("invalid hot signal socket path");
    fd_ = ::socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd_ < 0) throw std::runtime_error("cannot create hot signal datagram socket");
    if (::fcntl(fd_, F_SETFL, O_NONBLOCK) < 0 || ::fcntl(fd_, F_SETFD, FD_CLOEXEC) < 0) {
        const int error = errno; ::close(fd_); fd_ = -1;
        throw std::runtime_error("cannot configure hot signal datagram socket: " + std::string(std::strerror(error)));
    }
}

HotSignalDatagramSender::~HotSignalDatagramSender() { if (fd_ >= 0) ::close(fd_); }

bool HotSignalDatagramSender::send(std::string_view payload) noexcept {
    if (fd_ < 0 || payload.empty() || payload.size() > 60 * 1024) {
        errors_.fetch_add(1, std::memory_order_relaxed); return false;
    }
    sockaddr_un address{}; address.sun_family = AF_UNIX;
    const auto text = destination_.string();
    std::memcpy(address.sun_path, text.c_str(), text.size() + 1);
    const auto rc = ::sendto(fd_, payload.data(), payload.size(), MSG_DONTWAIT,
                             reinterpret_cast<const sockaddr*>(&address), sizeof(address));
    if (rc == static_cast<ssize_t>(payload.size())) {
        sent_.fetch_add(1, std::memory_order_relaxed); return true;
    }
    if (rc < 0 && (errno == ENOENT || errno == ECONNREFUSED || errno == EAGAIN || errno == EWOULDBLOCK)) {
        unavailable_.fetch_add(1, std::memory_order_relaxed); return false;
    }
    errors_.fetch_add(1, std::memory_order_relaxed); return false;
}

} // namespace pm::v7
