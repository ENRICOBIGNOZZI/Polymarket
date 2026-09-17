#pragma once

#include <cerrno>
#include <cstddef>
#include <vector>

#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#endif

namespace pm::threading {

// Cold-path discovery only. The returned IDs are already restricted by the
// process/cgroup affinity mask, so role placement never escapes its container.
inline std::vector<int> allowed_cpu_ids() {
#if defined(__linux__)
    cpu_set_t mask;
    CPU_ZERO(&mask);
    if (::sched_getaffinity(0, sizeof(mask), &mask) != 0) return {};
    std::vector<int> out;
    out.reserve(CPU_COUNT(&mask));
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
        if (CPU_ISSET(cpu, &mask)) out.push_back(cpu);
    }
    return out;
#else
    return {};
#endif
}

// HFT hosts are provisioned without SMT. Keep the low-numbered CPUs available
// for the OS/housekeeping plane and place hot roles on the tail of the allowed
// set. If the host cannot supply every requested role, do not partially pin.
inline std::vector<int> select_dedicated_cpus(const std::vector<int>& allowed,
                                               std::size_t count) {
    if (count == 0 || allowed.size() < count) return {};
    return {allowed.end() - static_cast<std::ptrdiff_t>(count), allowed.end()};
}

// Returns 0 on success, otherwise a POSIX-style error code. No allocation.
inline int pin_current_thread_to_cpu(int cpu) noexcept {
#if defined(__linux__)
    if (cpu < 0 || cpu >= CPU_SETSIZE) return EINVAL;
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(cpu, &mask);
    return ::pthread_setaffinity_np(::pthread_self(), sizeof(mask), &mask);
#else
    (void)cpu;
    return ENOTSUP;
#endif
}

} // namespace pm::threading
