#include "pm/rewards.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    assert(std::abs(pm::reward_spread_price_from_cents(3.0) - 0.03) < 1e-12);
    assert(pm::reward_spread_price_from_cents(-1.0) == 0.0);

    const double tight = pm::reward_order_score(0.03, 0.01, 100.0);
    assert(std::abs(tight - (4.0 / 9.0) * 100.0) < 1e-12);
    assert(pm::reward_order_score(0.03, 0.03, 100.0) == 0.0);
    assert(pm::reward_order_score(0.03, 0.01, 0.0) == 0.0);

    assert(std::abs(pm::reward_minimum_score(90.0, 60.0, 0.50) - 60.0) < 1e-12);
    assert(std::abs(pm::reward_minimum_score(90.0, 0.0, 0.50) - 30.0) < 1e-12);
    assert(pm::reward_minimum_score(90.0, 0.0, 0.05) == 0.0);

    const double share = pm::estimated_reward_share(10.0, 20.0, 2.0);
    assert(std::abs(share - 0.2) < 1e-12);
    assert(pm::estimated_reward_share(0.0, 0.0, 2.0) == 0.0);

    std::cout << "reward scoring tests passed\n";
}
