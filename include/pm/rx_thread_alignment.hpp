#pragma once

#include "pm/socket_tuning.hpp"
#include "pm/thread_tuning.hpp"

#include <algorithm>
#include <cstdint>
#include <span>

namespace pm::network {

struct RxThreadAlignmentObservation {
    int incoming_cpu = -1;
    int incoming_napi_id = -1;
    int pin_error = 0;
    std::uint8_t cpu_changed = 0;
    std::uint8_t applied = 0;
    std::uint8_t rejected = 0;
};

inline bool rx_cpu_allowed(int cpu, std::span<const int> allowlist) noexcept {
    return cpu >= 0 && std::find(allowlist.begin(), allowlist.end(), cpu) != allowlist.end();
}

inline RxThreadAlignmentObservation align_thread_to_socket_rx(
    int fd, std::span<const int> allowlist, int previous_incoming_cpu) noexcept {
    RxThreadAlignmentObservation out;
    out.incoming_cpu = incoming_cpu(fd);
    out.incoming_napi_id = incoming_napi_id(fd);
    if (out.incoming_cpu < 0 || out.incoming_cpu == previous_incoming_cpu) return out;
    out.cpu_changed = 1;
    if (!rx_cpu_allowed(out.incoming_cpu, allowlist)) {
        out.rejected = 1;
        return out;
    }
    out.pin_error = pm::threading::pin_current_thread_to_cpu(out.incoming_cpu);
    if (out.pin_error == 0) out.applied = 1;
    return out;
}

} // namespace pm::network
