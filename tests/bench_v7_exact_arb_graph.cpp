#include "pm/v7_exact_arb_graph_hotpath.hpp"
#include <chrono>
#include <cassert>
#include <iostream>
#include <vector>

using namespace pm::v7;
using namespace pm::v7::exact_arb_graph;
volatile std::int64_t benchmark_sink = 0;

template<class F> double measure(const char* name, std::size_t iterations, F fn) {
    std::vector<std::int64_t> samples; samples.reserve(iterations);
    for (std::size_t i = 0; i < 2000; ++i) benchmark_sink = fn(i);
    for (std::size_t i = 0; i < iterations; ++i) {
        const auto begin = std::chrono::steady_clock::now();
        benchmark_sink = fn(i);
        const auto end = std::chrono::steady_clock::now();
        samples.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end-begin).count());
    }
    std::sort(samples.begin(), samples.end());
    auto at = [&](double p) {return samples[static_cast<std::size_t>(p*(samples.size()-1))];};
    std::cout << '"' << name << "\":{\"p50\":" << at(.5) << ",\"p90\":" << at(.9)
              << ",\"p99\":" << at(.99) << ",\"p99_9\":" << at(.999) << ",\"max\":" << samples.back() << '}';
    return at(.99);
}

int main(int argc, char** argv) {
    const std::size_t iterations = argc > 1 ? std::stoul(argv[1]) : 100000;
    if (iterations < 10000 || iterations > 10000000) return 2;
    std::array<BookDeepSnapshot, kMaxLegs> books{};
    auto prepare = [&](std::size_t n) {
        CompiledRelation r{}; r.enabled = 1; r.proof_handle = 1;
        r.leg_count = static_cast<std::uint8_t>(n); r.guaranteed_payout_microunits = 1000000;
        r.reserve_per_unit_microunits = 500;
        for (std::size_t i = 0; i < n; ++i) {
            r.legs[i].book_handle = static_cast<std::uint32_t>(i);
            r.legs[i].coefficient = {1,1}; r.legs[i].fee_verified = 1; r.legs[i].fee_rate = .02;
            books[i].valid = books[i].lineage_continuous = 1; books[i].ask_level_count = 4;
            for (int j = 0; j < 4; ++j) books[i].ask_levels[j] = {static_cast<int>(9000/n)+j*10, 1000000*(j+1)};
        }
        for (int variation=0;variation<2;++variation) {
            books[0].ask_levels[0].price_e4=9000/n+variation;
            assert(evaluate_buy_basket(r,books).reject==HotReject::Accepted);
        }
        return r;
    };
    auto r = prepare(2);
    std::cout << "{\"schema\":\"polymarket_v7_exact_arb_native_benchmark_v1\",\"iterations\":" << iterations
              << ",\"units\":\"nanoseconds\",\"depth_levels\":4,\"timed_io\":false,\"measurements\":{";
    const auto champion = measure("champion_binary_sweep", iterations, [&](std::size_t i) {
        books[0].ask_levels[0].price_e4 = 4500 + static_cast<int>(i%2);
        const auto c = pure_arb::sweep(books[0], books[1], .02, 1.0, .0005, true);
        return c.shares_microunits + std::llround(c.conservative_locked_pnl*1e6);
    });
    std::cout << ',';
    const auto binary = measure("graph_binary_full_depth_sizing", iterations, [&](std::size_t i) {
        books[0].ask_levels[0].price_e4 = 4500 + static_cast<int>(i%2);
        const auto g = evaluate_buy_basket(r, books);
        return g.quantity_microunits + g.net_pnl_microunits;
    });
    for (auto n : {3,4,8,16}) {
        r = prepare(n); std::cout << ',';
        const auto name = "graph_"+std::to_string(n)+"_leg_full_depth_sizing";
        measure(name.c_str(), iterations, [&](std::size_t i) {
            books[0].ask_levels[0].price_e4 = 9000/n + static_cast<int>(i%2);
            return evaluate_buy_basket(r, books).net_pnl_microunits;
        });
    }
    std::array<TokenDependency, 1024> dependencies{};
    for (std::uint32_t i = 0; i < dependencies.size(); ++i) dependencies[i] = {i,0,1};
    std::cout << ',';
    measure("dependency_lookup", iterations, [&](std::size_t i) {
        const auto token = static_cast<std::uint32_t>(i%1024);
        return std::lower_bound(dependencies.begin(), dependencies.end(), token,
            [](auto d, auto t) {return d.token_handle < t;})->relation_count;
    });
    r = prepare(2);
    std::array<CompiledRelation,1> relations{r}; std::array<std::uint32_t,1> handles{0};
    std::cout << ',';
    measure("graph_binary_total_decision_compute", iterations, [&](std::size_t i) {
        std::int64_t pnl = 0;
        books[0].ask_levels[0].price_e4 = 4500 + static_cast<int>(i%2);
        evaluate_token_update(static_cast<std::uint32_t>(i%1024),dependencies,handles,relations,books,{},
                              [&](auto decision){pnl=decision.net_pnl_microunits;});
        return pnl;
    });
    std::cout << "},\"graph_binary_over_champion_p99\":" << (champion ? binary/champion : 0) << "}\n";
}
