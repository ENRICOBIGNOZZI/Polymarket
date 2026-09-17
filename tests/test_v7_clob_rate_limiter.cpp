#include "pm/v7_clob_rate_limiter.hpp"

#include <cassert>
#include <cstdint>

using namespace pm::v7::clob;

int main() {
    ClobRateLimiter limiter;
    constexpr std::int64_t t0 = 1'000'000'000LL;

    for (int i = 0; i < 54; ++i)
        assert(limiter.admit(RequestClass::Order, 1.0, t0).admitted != 0);
    const auto order_blocked = limiter.admit(RequestClass::Order, 1.0, t0);
    assert(order_blocked.admitted == 0 && order_blocked.next_admissible_ns > t0);

    // Order exhaustion must never consume the independent cancel bucket.
    for (int i = 0; i < 108; ++i)
        assert(limiter.admit(RequestClass::Cancel, 1.0, t0).admitted != 0);
    assert(limiter.admit(RequestClass::Cancel, 1.0, t0).admitted == 0);

    constexpr std::int64_t t1 = t0 + 1'000'000'000LL;
    for (int i = 0; i < 36; ++i)
        assert(limiter.admit(RequestClass::Order, 1.0, t1).admitted != 0);
    assert(limiter.admit(RequestClass::Order, 1.0, t1).admitted == 0);

    ClobRateLimiter batch;
    assert(batch.admit(RequestClass::Order, 50.0, t0).admitted != 0);
    assert(batch.admit(RequestClass::Order, 5.0, t0).admitted == 0);

    ClobRateLimiter remote;
    remote.clamp_signer_remaining(RequestClass::Order, 2.0, t0);
    assert(remote.admit(RequestClass::Order, 2.0, t0).admitted != 0);
    assert(remote.admit(RequestClass::Order, 1.0, t0).admitted == 0);
    return 0;
}
