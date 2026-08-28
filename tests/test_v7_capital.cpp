#include "pm/v7_capital.hpp"

#include <cassert>

namespace {

pm::v7::CapitalLimits limits() {
    pm::v7::CapitalLimits out;
    out.sleeve_budget_microdollars = 10'000'000;
    out.max_total_exposure_microdollars = 2'000'000;
    out.max_market_exposure_microdollars = 750'000;
    out.max_single_order_microdollars = 250'000;
    return out;
}

void test_limits_and_idempotent_reservation() {
    pm::v7::SleeveCapitalAccount capital(limits());
    assert(capital.limits().valid());
    assert(capital.reserve_order(101, 1, 200'000));
    assert(capital.reserve_order(101, 1, 200'000));
    assert(!capital.reserve_order(101, 1, 199'999));
    assert(!capital.reserve_order(101, 2, 200'000));
    const auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 200'000);
    assert(snap.total_exposure_microdollars == 200'000);
}

void test_single_market_and_total_limits_fail_closed() {
    pm::v7::SleeveCapitalAccount capital(limits());
    assert(!capital.reserve_order(1, 1, 250'001));
    assert(capital.reserve_order(2, 1, 250'000));
    assert(capital.reserve_order(3, 1, 250'000));
    assert(capital.reserve_order(4, 1, 250'000));
    assert(!capital.reserve_order(5, 1, 1));

    assert(capital.reserve_order(6, 2, 250'000));
    assert(capital.reserve_order(7, 2, 250'000));
    assert(capital.reserve_order(8, 2, 250'000));
    assert(capital.reserve_order(9, 3, 250'000));
    assert(capital.reserve_order(10, 4, 250'000));
    assert(!capital.reserve_order(11, 5, 1));
    assert(capital.snapshot().total_exposure_microdollars == 2'000'000);
}

void test_settlement_converts_order_to_inventory_without_double_counting() {
    pm::v7::SleeveCapitalAccount capital(limits());
    assert(capital.reserve_order(11, 3, 240'000));
    assert(capital.settle_order_to_inventory(11, 180'000));
    const auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 0);
    assert(snap.inventory_committed_microdollars == 180'000);
    assert(snap.total_exposure_microdollars == 180'000);
    assert(capital.inventory_committed_for_market(3) == 180'000);
    assert(!capital.release_order(11));
}

void test_inventory_commit_release_and_reconfiguration_safety() {
    pm::v7::SleeveCapitalAccount capital(limits());
    assert(capital.commit_inventory(4, 400'000));
    assert(!capital.configure(limits()));
    assert(!capital.release_inventory(4, 400'001));
    assert(capital.release_inventory(4, 150'000));
    assert(capital.inventory_committed_for_market(4) == 250'000);
    assert(capital.release_inventory(4, 250'000));
    assert(capital.configure(limits()));
}

void test_release_reuses_fixed_capacity_slots() {
    pm::v7::SleeveCapitalAccount capital(limits());
    for (std::uint64_t i = 1; i <= 8; ++i) {
        assert(capital.reserve_order(1000 + i, i, 100'000));
        assert(capital.release_order(1000 + i));
    }
    assert(capital.reserve_order(9001, 9, 100'000));
    assert(capital.order_reserved_for_market(9) == 100'000);
}

} // namespace

int main() {
    test_limits_and_idempotent_reservation();
    test_single_market_and_total_limits_fail_closed();
    test_settlement_converts_order_to_inventory_without_double_counting();
    test_inventory_commit_release_and_reconfiguration_safety();
    test_release_reuses_fixed_capacity_slots();
    return 0;
}
