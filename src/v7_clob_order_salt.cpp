#include "pm/v7_clob_order_salt.hpp"

#include <limits>
#include <cstdlib>
#if defined(__linux__)
#include <cerrno>
#include <sys/random.h>
#endif

namespace pm::v7::clob_order {

OrderSaltSequence::OrderSaltSequence(std::uint64_t seed) noexcept
    : seed_(seed), current_(seed), valid_(seed != 0) {}

namespace {
[[nodiscard]] bool fill_os_entropy(std::uint64_t& seed) noexcept {
#if defined(__APPLE__)
    ::arc4random_buf(&seed, sizeof(seed));
    return true;
#elif defined(__linux__)
    unsigned char* out = reinterpret_cast<unsigned char*>(&seed);
    std::size_t done = 0;
    while (done < sizeof(seed)) {
        const auto rc = ::getrandom(out + done, sizeof(seed) - done, 0);
        if (rc > 0) { done += static_cast<std::size_t>(rc); continue; }
        if (rc < 0 && errno == EINTR) continue;
        return false;
    }
    return true;
#else
    (void)seed;
    return false;
#endif
}
} // namespace

OrderSaltSequence OrderSaltSequence::from_os_entropy() noexcept {
    std::uint64_t seed = 0;
    for (int attempt = 0; attempt < 4; ++attempt) {
        if (!fill_os_entropy(seed)) return {};
        if (seed != 0) return OrderSaltSequence(seed);
    }
    return {};
}

std::uint64_t OrderSaltSequence::next() noexcept {
    if (!valid_ || exhausted_) return 0;
    const std::uint64_t out = current_;
    if (current_ == std::numeric_limits<std::uint64_t>::max()) current_ = 1;
    else ++current_;
    if (current_ == seed_) exhausted_ = true;
    return out;
}

} // namespace pm::v7::clob_order
