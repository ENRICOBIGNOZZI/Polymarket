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

void test_terminal_settlement_converts_order_to_inventory_and_releases_residual() {
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

void test_partial_fill_preserves_residual_reservation_until_terminal_cancel() {
    pm::v7::SleeveCapitalAccount capital(limits());
    assert(capital.reserve_order(21, 5, 240'000));
    const auto before = capital.snapshot();
    assert(before.total_exposure_microdollars == 240'000);

    assert(capital.settle_partial_fill(21, 90'000));
    auto after_partial = capital.snapshot();
    assert(after_partial.order_reserved_microdollars == 150'000);
    assert(after_partial.inventory_committed_microdollars == 90'000);
    assert(after_partial.total_exposure_microdollars == 240'000);
    assert(capital.order_reserved_for_market(5) == 150'000);
    assert(capital.inventory_committed_for_market(5) == 90'000);

    // Another partial is also exposure-neutral.
    assert(capital.settle_partial_fill(21, 50'000));
    after_partial = capital.snapshot();
    assert(after_partial.order_reserved_microdollars == 100'000);
    assert(after_partial.inventory_committed_microdollars == 140'000);
    assert(after_partial.total_exposure_microdollars == 240'000);

    // Once cancel is terminal the unfilled residual may finally be released.
    assert(capital.release_order(21));
    const auto terminal = capital.snapshot();
    assert(terminal.order_reserved_microdollars == 0);
    assert(terminal.inventory_committed_microdollars == 140'000);
    assert(terminal.total_exposure_microdollars == 140'000);
}

void test_full_fill_via_partial_api_retires_reservation() {
    pm::v7::SleeveCapitalAccount capital(limits());
    assert(capital.reserve_order(31, 6, 200'000));
    assert(capital.settle_partial_fill(31, 200'000));
    const auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 0);
    assert(snap.inventory_committed_microdollars == 200'000);
    assert(!capital.release_order(31));
}

void test_partial_fill_rejects_overcommit_without_mutation() {
    pm::v7::SleeveCapitalAccount capital(limits());
    assert(capital.reserve_order(41, 7, 200'000));
    const auto version = capital.snapshot().state_version;
    assert(!capital.settle_partial_fill(41, 200'001));
    const auto snap = capital.snapshot();
    assert(snap.order_reserved_microdollars == 200'000);
    assert(snap.inventory_committed_microdollars == 0);
    assert(snap.state_version == version);
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
    test_terminal_settlement_converts_order_to_inventory_and_releases_residual();
    test_partial_fill_preserves_residual_reservation_until_terminal_cancel();
    test_full_fill_via_partial_api_retires_reservation();
    test_partial_fill_rejects_overcommit_without_mutation();
    test_inventory_commit_release_and_reconfiguration_safety();
    test_release_reuses_fixed_capacity_slots();
    return 0;
}
