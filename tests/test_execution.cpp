#include "pm/execution.hpp"
#include <cassert>
#include <cmath>
#include <iostream>
#include <vector>

int main() {
    auto a = pm::consume_passive_buy(10.0, 5.0, 0.40, 0.40, 6.0, true);
    assert(std::abs(a.queue_ahead - 4.0) < 1e-12);
    assert(a.fill_shares == 0.0);

    auto b = pm::consume_passive_buy(4.0, 5.0, 0.40, 0.39, 7.0, true);
    assert(b.queue_ahead == 0.0);
    assert(std::abs(b.fill_shares - 3.0) < 1e-12);

    auto c = pm::consume_passive_buy(0.0, 5.0, 0.40, 0.41, 100.0, true);
    assert(c.fill_shares == 0.0);
    auto d = pm::consume_passive_buy(0.0, 5.0, 0.40, 0.39, 100.0, false);
    assert(d.fill_shares == 0.0);

    std::vector<std::pair<double,double>> legs{{9.5,10.0},{19.0,20.0},{5.0,5.0}};
    assert(std::abs(pm::minimum_completion(legs)-0.95)<1e-12);
    std::cout << "execution tests passed\n";
}
