#include "pm/v7_probability_ev.hpp"
#include <cassert>
#include <iostream>
using namespace pm::v7;
int main() {
    SettlementProbabilityForecast f{.up=.75,.lower=.70,.upper=.80,
        .asof_ns=100,.max_input_receive_ns=99,.valid_until_ns=200,.version=1,.valid=1};
    ProbabilityEvPolicy p;
    p.allocated_wealth_microdollars=100'000'000;p.available_microdollars=100'000'000;
    ProbabilityEvQuote q{.ask_e4=6000,.tick_size_e4=100,.maximum_limit_e4=7500,
        .visible_quantity_microunits=100'000'000,
        .minimum_quantity_microunits=5'000'000,.fee_rate=.07,.fee_exponent=1,
        .execution_reserve_per_share=.005,.is_up=1};
    auto d=evaluate_probability_ev(f,q,p,150);
    assert(d.accepted && d.quantity_microunits>=5'000'000);
    assert(d.cost_ceiling_microdollars<=p.max_order_cost_microdollars);
    auto bad=f;bad.up=.60;bad.lower=.55;bad.upper=.65;
    q.ask_e4=6500;assert(evaluate_probability_ev(bad,q,p,150).reason==ProbabilityEvReason::NonPositiveNetEdge);
    q.is_up=0;q.ask_e4=2100;
    assert(evaluate_probability_ev(f,q,p,150).reason==ProbabilityEvReason::NonPositiveNetEdge);
    q.ask_e4=1000;d=evaluate_probability_ev(f,q,p,150);
    assert(d.accepted && std::abs(d.probability_lower-.20)<1e-12);
    auto unknown=q;unknown.fee_rate=std::numeric_limits<double>::quiet_NaN();
    assert(evaluate_probability_ev(f,unknown,p,150).reason==ProbabilityEvReason::UnknownCost);
    bad=f;bad.max_input_receive_ns=151;assert(!evaluate_probability_ev(bad,q,p,150).accepted);
    assert(evaluate_probability_ev(f,q,p,201).reason==ProbabilityEvReason::StaleForecast);
    p.max_order_cost_microdollars=100'000;
    assert(evaluate_probability_ev(f,q,p,150).reason==ProbabilityEvReason::BelowVenueMinimum);
    p.max_order_cost_microdollars=3'750'000;
    q.visible_quantity_microunits=4'999'999;
    assert(evaluate_probability_ev(f,q,p,150).reason==ProbabilityEvReason::BelowVenueMinimum);
    q.visible_quantity_microunits=100'000'000;
    p.maximum_chase_ticks=2;
    q.ask_e4=1000; q.maximum_limit_e4=7500;
    const auto chased=evaluate_probability_ev(f,q,p,150);
    assert(chased.accepted);
    assert(chased.maximum_executable_price_e4==1200);
    assert(chased.worst_case_cost_per_share>chased.cost_per_share);
    assert(chased.worst_case_conservative_net_edge<chased.conservative_net_edge);
    assert(chased.cost_ceiling_microdollars<=p.max_order_cost_microdollars);
    const auto low=evaluate_probability_ev(f,q,p,150);
    auto stronger=f;stronger.up=.50;stronger.lower=.45;stronger.upper=.55;
    const auto high=evaluate_probability_ev(stronger,q,p,150);
    assert(high.quantity_microunits>=low.quantity_microunits);
    for(int price=1;price<10000;price+=11) {
        q.ask_e4=price;
        const auto x=evaluate_probability_ev(f,q,p,150);
        if(x.accepted) {
            assert(x.conservative_net_edge>0);
            assert(x.cost_ceiling_microdollars<=p.max_order_cost_microdollars);
            assert(x.quantity_microunits>=q.minimum_quantity_microunits);
            assert(x.quantity_microunits<=q.visible_quantity_microunits);
        }
    }
    std::cout<<"probability EV, complement bounds, fee/missingness, causal clocks and hard-risk-cap tests passed\n";
}
