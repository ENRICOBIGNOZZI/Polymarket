#include "pm/v7_signer_rate_limit.hpp"

#include <cassert>

namespace {

void test_each_signer_has_independent_budget() {
    pm::v7::SignerRateLimiter limiter;
    assert(limiter.register_signer(1));
    assert(limiter.register_signer(2));
    for (int i = 0; i < 10; ++i) assert(limiter.admit(1, pm::v7::SignerRequestClass::NewOrder, 100 + i));
    assert(!limiter.admit(1, pm::v7::SignerRequestClass::NewOrder, 200));
    assert(limiter.admit(2, pm::v7::SignerRequestClass::NewOrder, 200));
}

void test_reserved_emergency_capacity_survives_submission_burst() {
    pm::v7::SignerRateLimiter limiter;
    assert(limiter.register_signer(1));
    for (int i = 0; i < 10; ++i) assert(limiter.admit(1, pm::v7::SignerRequestClass::NewOrder, 100 + i));
    for (int i = 0; i < 10; ++i) assert(limiter.admit(1, pm::v7::SignerRequestClass::CancelOrHeartbeat, 200 + i));
    assert(!limiter.admit(1, pm::v7::SignerRequestClass::CancelOrHeartbeat, 300));
    const auto snapshot = limiter.snapshot(1, 300);
    assert(snapshot.regular_in_window == 10);
    assert(snapshot.emergency_in_window == 10);
    assert(snapshot.total_in_window == 20);
}

void test_rolling_window_releases_budget_without_clock_regression() {
    pm::v7::SignerRateLimiter limiter;
    assert(limiter.register_signer(1));
    for (int i = 0; i < 10; ++i) assert(limiter.admit(1, pm::v7::SignerRequestClass::NewOrder, 100 + i));
    assert(limiter.admit(1, pm::v7::SignerRequestClass::NewOrder, 1'000'000'100LL));
}

void test_clock_regression_quarantines_only_the_affected_signer() {
    pm::v7::SignerRateLimiter limiter;
    assert(limiter.register_signer(1));
    assert(limiter.register_signer(2));
    assert(limiter.admit(1, pm::v7::SignerRequestClass::NewOrder, 100));
    assert(!limiter.admit(1, pm::v7::SignerRequestClass::NewOrder, 99));
    assert(limiter.snapshot(1, 100).fail_closed == 1);
    assert(limiter.admit(2, pm::v7::SignerRequestClass::NewOrder, 100));
}

} // namespace

int main() {
    test_each_signer_has_independent_budget();
    test_reserved_emergency_capacity_survives_submission_burst();
    test_rolling_window_releases_budget_without_clock_regression();
    test_clock_regression_quarantines_only_the_affected_signer();
    return 0;
}
