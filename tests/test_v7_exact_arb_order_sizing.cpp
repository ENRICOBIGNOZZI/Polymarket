#include "pm/v7_exact_arb_order_sizing.hpp"
#include <boost/multiprecision/cpp_int.hpp>
#include <boost/rational.hpp>
#include <cassert>
#include <iostream>
#include <fstream>
#include <memory>
#include <tuple>
#include <boost/json.hpp>

using namespace pm::v7;
using namespace pm::v7::exact_arb_graph;
using Big = boost::multiprecision::cpp_int;
using Rat = boost::rational<Big>;

struct Reference { Rat raw{}, fees{}, reserve{}, capital{}, net{}; bool feasible = false; };
// Independent arbitrary-precision rational oracle: no native fee/sizing helper.
Reference reference(const CompiledRelation& r, std::span<const BookDeepSnapshot> books,
                    HotResources resources, std::int64_t q, std::int64_t lot) {
    Reference out; Rat cash;
    for (unsigned i=0; i<r.leg_count; ++i) {
        const auto& l=r.legs[i];
        Rat shares(Big(q)*l.coefficient.numerator, l.coefficient.denominator);
        if (shares.denominator()!=1 || shares.numerator()%lot!=0 || shares< Rat(l.minimum_order_microunits)) return out;
        if (r.sell_inventory && (l.book_handle>=resources.inventory.size() || shares>Rat(resources.inventory[l.book_handle]))) return out;
        const auto& b=books[l.book_handle];
        const auto& levels=r.sell_inventory?b.bid_levels:b.ask_levels;
        const auto count=r.sell_inventory?b.bid_level_count:b.ask_level_count;
        Rat left=shares/Rat(1000000);
        for (unsigned j=0;j<count && left>0;++j) {
            Rat take=std::min(left,Rat(levels[j].quantity_microunits,1000000));
            Rat price(levels[j].price_e4,10000);
            cash+=take*price;
            Rat fee=take*Rat(static_cast<std::int64_t>(std::llround(l.fee_rate*1e9)),1000000000);
            for (int e=0;e<l.fee_exponent;++e) fee*=price*(Rat(1)-price);
            if (fee>=Rat(1,100000)) {
                Rat rounded=fee*Rat(100000)+Rat(1,2);
                out.fees+=Rat(rounded.numerator()/rounded.denominator(),100000);
            }
            left-=take;
        }
        if (left!=0) return out;
    }
    const Rat payout(Big(q)*r.guaranteed_payout_microunits,Big(1000000000000LL));
    out.raw=r.sell_inventory?cash-payout:payout-cash;
    out.reserve=Rat(Big(q)*r.reserve_per_unit_microunits,Big(1000000000000LL));
    out.net=out.raw-out.fees-out.reserve;
    out.capital=(r.sell_inventory?Rat(0):cash+out.fees)+out.reserve;
    out.feasible=out.capital<=Rat(resources.capital_microunits,1000000)
        && q<=resources.quantity_limit_microunits
        && (!resources.transformation || q<=resources.transformation->capacity_microunits);
    return out;
}

std::int64_t floor_micro(Rat x) {
    x*=Rat(1000000); Big n=x.numerator()/x.denominator();
    if (x<0 && x.numerator()%x.denominator()!=0) --n;
    return n.convert_to<std::int64_t>();
}

std::int64_t round_positive_micro(const Rat& x) {
    assert(x>=0);
    const Rat scaled=x*Rat(1000000)+Rat(1,2);
    return (scaled.numerator()/scaled.denominator()).convert_to<std::int64_t>();
}

std::string rational_text(const Rat& x) {
    return x.numerator().convert_to<std::string>()+"/"+x.denominator().convert_to<std::string>();
}

// Counterfactual accounting with exactly the champion's shared basket slices,
// but arbitrary-precision arithmetic. Neither venue semantics nor a new lane.
Big slice_fee_units(std::int64_t quantity, std::int32_t price, std::int64_t nanorate) {
    // Common denominator avoids repeated rational gcd work at every lattice
    // point. Still independent arbitrary precision, never a production helper.
    const Big numerator=Big(quantity)*nanorate*price*(10000-price);
    const Big denominator=Big(1000000000000000000LL);
    if (numerator<denominator) return Big(0);
    return (numerator+denominator/2)/denominator;
}

struct SliceResult { std::int64_t q=0; Rat net{}; };
SliceResult basket_slices(const std::array<BookDeepSnapshot,kMaxLegs>& books,
                         std::int64_t rate, std::int64_t cap, bool greedy) {
    SliceResult out;
    Big total_pico=0;
    unsigned y=0,n=0;
    auto yr=books[0].ask_levels[0].quantity_microunits;
    auto nr=books[1].ask_levels[0].quantity_microunits;
    while (y<books[0].ask_level_count && n<books[1].ask_level_count && out.q<cap) {
        const auto take=std::min({yr,nr,cap-out.q});
        const auto yp=books[0].ask_levels[y].price_e4, np=books[1].ask_levels[n].price_e4;
        const Big net_pico=Big(take)*(10000-yp-np)*100-Big(take)*500
            -(slice_fee_units(take,yp,rate)+slice_fee_units(take,np,rate))*10000000;
        // Champion's explicit epsilon is part of its stopping rule.
        if (greedy && net_pico*1000000<=take) break;
        out.q+=take;total_pico+=net_pico;
        yr-=take;nr-=take;
        if (!yr && ++y<books[0].ask_level_count) yr=books[0].ask_levels[y].quantity_microunits;
        if (!nr && ++n<books[1].ask_level_count) nr=books[1].ask_levels[n].quantity_microunits;
    }
    out.net=Rat(total_pico,Big(1000000000000LL));
    return out;
}

void same_sizing(const OrderSizing& a,const OrderSizing& b) {
    auto signature=[](const OrderSizing& x) {
        return std::tie(x.decision.reject,x.decision.relation_handle,x.decision.quantity_microunits,
            x.decision.gross_pnl_microunits,x.decision.net_pnl_microunits,x.decision.raw_pnl_microunits,
            x.decision.fees_microunits,x.decision.capital_required_microunits,x.decision.levels_consumed,x.decision.levels_used,
            x.unconstrained_quantity_microunits,x.relation_quantum_microunits,x.quantity_precision_ready,
            x.global_optimum_proven,x.search_exhausted,x.quantities_evaluated,
            x.net_upper_bound_microunits,x.net_upper_bound_valid);
    };
    assert(signature(a)==signature(b));
}

int main(int argc, char** argv) {
    // Optional immutable research artifact; normal CTest remains assertion-only.
    assert(argc==1 || (argc==3 && std::string(argv[1])=="--attribution-report"));
    namespace json=boost::json;
    json::array attributions;
    std::array<BookDeepSnapshot,kMaxLegs> books{};
    auto workspace=std::make_unique<order_sizing_detail::DepthWorkspace>();
    CompiledRelation r; r.enabled=1; r.leg_count=2;
    r.guaranteed_payout_microunits=1000000; r.reserve_per_unit_microunits=500;
    for (unsigned i=0;i<2;++i) {
        r.legs[i].book_handle=i; r.legs[i].coefficient={1,1}; r.legs[i].fee_verified=1;
        r.legs[i].fee_rate=.0005; r.legs[i].fee_exponent=0;
        books[i].valid=books[i].lineage_continuous=1;
        books[i].ask_level_count=1; books[i].ask_levels[0]={i?4991:5000,100000};
    }
    // Whole-depth negative, small whole order positive due to the documented
    // fee cutoff in this model. A first-negative-marginal sweep misses it.
    assert(evaluate_buy_basket(r,books).reject==HotReject::NoPositiveEdge);
    auto size=evaluate_order_constrained_basket(r,books,{}, {},10000);
    assert(size.global_optimum_proven && size.decision.reject==HotReject::Accepted);
    assert(size.decision.quantity_microunits==10000 && size.decision.net_pnl_microunits==4);
    assert(size.decision.fees_microunits==0);
    auto exhausted=evaluate_order_constrained_basket(r,books,{}, {},10000,1);
    assert(exhausted.search_exhausted && !exhausted.global_optimum_proven);
    assert(exhausted.decision.reject==HotReject::Accepted && exhausted.quantities_evaluated==1);
    exhausted=evaluate_order_constrained_basket(r,books,{}, {},10000,0);
    assert(exhausted.decision.reject==HotReject::SizingIncomplete && !exhausted.global_optimum_proven);

    // One cheap microshare must not disappear because the relation coefficient
    // is two. Fill fragments are NOT subject to the total order quantum.
    r.legs[0].coefficient={2,1}; r.legs[0].fee_rate=r.legs[1].fee_rate=0;
    books[0].ask_level_count=2; books[0].ask_levels[0]={1000,1}; books[0].ask_levels[1]={2000,19999};
    books[1].ask_levels[0]={4000,10000};
    size=evaluate_order_constrained_basket(r,books,{}, {},10000);
    assert(size.global_optimum_proven && size.decision.quantity_microunits==10000);
    assert(size.decision.levels_consumed[0]==2 && size.decision.net_pnl_microunits==1995);

    // Another leg changing level must not create artificial fee fragments.
    r.legs[0].coefficient={1,1};
    for (unsigned i=0;i<2;++i) { r.legs[i].fee_rate=.001; r.legs[i].fee_exponent=0; }
    books[0].ask_level_count=1; books[0].ask_levels[0]={4000,10000};
    books[1].ask_level_count=2; books[1].ask_levels[0]={4000,5000}; books[1].ask_levels[1]={4001,5000};
    size=evaluate_order_constrained_basket(r,books,{}, {},10000);
    assert(size.decision.fees_microunits==10);
    assert(evaluate_buy_basket(r,books).fees_microunits==0);

    auto invalid_books=books; invalid_books[0].ask_truncated=1;
    assert(evaluate_order_constrained_basket(r,invalid_books,{}, {},10000).decision.reject==HotReject::IncompleteDepth);
    invalid_books=books; invalid_books[0].lineage_continuous=0;
    assert(evaluate_order_constrained_basket(r,invalid_books,{}, {},10000).decision.reject==HotReject::IncompleteDepth);
    auto invalid_relation=r; invalid_relation.legs[0].fee_verified=0;
    assert(evaluate_order_constrained_basket(invalid_relation,books,{}, {},10000).decision.reject==HotReject::UnknownFee);
    invalid_relation=r; invalid_relation.legs[0].fee_exponent=.5;
    assert(evaluate_order_constrained_basket(invalid_relation,books,{}, {},10000).decision.reject==HotReject::UnknownFee);
    invalid_relation=r; invalid_relation.legs[1].book_handle=0;
    assert(evaluate_order_constrained_basket(invalid_relation,books,{}, {},10000).decision.reject==HotReject::InvalidRelation);
    invalid_books=books; invalid_books[0].receive_monotonic_ns=100;invalid_books[1].receive_monotonic_ns=190;
    assert(evaluate_order_constrained_basket(r,invalid_books,{200,50,0}, {},10000).decision.reject==HotReject::StaleBook);
    assert(evaluate_order_constrained_basket(r,invalid_books,{200,150,50}, {},10000).decision.reject==HotReject::LegSkew);
    invalid_relation=r;invalid_relation.sell_inventory=1;
    invalid_books=books;
    for (auto& b : invalid_books) { b.bid_level_count=1;b.bid_levels[0]={6000,10000}; }
    assert(evaluate_order_constrained_basket(invalid_relation,invalid_books,{}, {},10000).decision.reject==HotReject::InventoryUnavailable);
    HotResources no_capital;no_capital.capital_microunits=0;
    assert(evaluate_order_constrained_basket(r,books,{},no_capital,10000).decision.reject==HotReject::CapitalLimit);
    CompiledTransformation transform;HotResources transformed;transformed.transformation=&transform;
    assert(evaluate_order_constrained_basket(r,books,{},transformed,10000).decision.reject==HotReject::TransformationUnavailable);
    transform.verified=1;transform.capacity_microunits=10000;transform.capital_lock_ns=100;
    assert(evaluate_order_constrained_basket(r,books,{},transformed,10000).decision.quantity_microunits==10000);
    invalid_relation=r;invalid_relation.legs[0].coefficient={1,INT64_MAX};
    assert(evaluate_order_constrained_basket(invalid_relation,books,{}, {},10000).decision.reject==HotReject::VenuePrecision);

    std::uint64_t seed=8732981;
    auto random=[&]() { seed^=seed<<13;seed^=seed>>7;seed^=seed<<17;return seed; };
    std::size_t proven=0, incomplete=0, champion_quantity_differences=0;
    for (int trial=0;trial<3000;++trial) {
        CompiledRelation t; t.enabled=1; t.leg_count=static_cast<std::uint8_t>(2+random()%15);
        t.sell_inventory=trial%2;
        t.guaranteed_payout_microunits=1000000;
        t.reserve_per_unit_microunits=static_cast<std::int64_t>(random()%20)*100;
        std::array<std::int64_t,kMaxLegs> inventory{};
        HotResources resources; resources.inventory=inventory;
        resources.quantity_limit_microunits=10000*(1+random()%40);
        resources.capital_microunits=trial%3?INT64_MAX:50000+random()%300000;
        for (unsigned i=0;i<t.leg_count;++i) {
            auto& l=t.legs[i]; l.book_handle=i; l.coefficient={1+static_cast<std::int64_t>(random()%3),1+static_cast<std::int64_t>(random()%3)};
            l.fee_verified=1; l.fee_rate=static_cast<double>(random()%5)/1000; l.fee_exponent=random()%3;
            l.minimum_order_microunits=10000*(random()%3);
            auto& b=books[i]; b={}; b.valid=b.lineage_continuous=1;
            b.ask_level_count=b.bid_level_count=3;
            const auto base=static_cast<int>(6000/t.leg_count+random()%100);
            for (int j=0;j<3;++j) {
                const auto qty=static_cast<std::int64_t>(1+random()%150000);
                b.ask_levels[j]={base+j*100,qty};
                b.bid_levels[j]={base+300-j*100,qty};
            }
            inventory[i]=20000+random()%400000;
        }
        if (trial%3==0) { // concentrate near raw break-even as well as wide edges
            Rat unit;
            for (unsigned i=0;i<t.leg_count;++i) {
                const auto p=t.sell_inventory?books[i].bid_levels[0].price_e4:books[i].ask_levels[0].price_e4;
                unit+=Rat(t.legs[i].coefficient.numerator,t.legs[i].coefficient.denominator)*Rat(p,10000);
            }
            t.guaranteed_payout_microunits=std::max<std::int64_t>(1,floor_micro(unit)+static_cast<std::int64_t>(random()%2000)-1000);
        }
        auto result=evaluate_order_constrained_basket(t,books,{},resources,10000,4096);
        same_sizing(result,evaluate_order_constrained_basket(t,books,{},resources,10000,4096,workspace.get()));
        Rat best; std::int64_t best_q=0; Reference best_ref;
        std::int64_t reference_quantum=1;
        for (unsigned i=0;i<t.leg_count;++i) {
            const Rat lots_per_micro(t.legs[i].coefficient.numerator,Big(t.legs[i].coefficient.denominator)*10000);
            reference_quantum=std::lcm(reference_quantum,lots_per_micro.denominator().convert_to<std::int64_t>());
        }
        assert(result.relation_quantum_microunits==reference_quantum);
        for (std::int64_t q=reference_quantum;q<=resources.quantity_limit_microunits;q+=reference_quantum) {
            auto ref=reference(t,books,resources,q,10000);
            if (ref.feasible && ref.net>best) { best=ref.net;best_q=q;best_ref=ref; }
        }
        if (result.global_optimum_proven) {
            ++proven;
            assert(result.net_upper_bound_valid);
            if (best_q) {
                assert(result.decision.quantity_microunits==best_q);
                assert(result.decision.net_pnl_microunits==floor_micro(best));
                assert(result.decision.raw_pnl_microunits==floor_micro(best_ref.raw));
                assert(result.decision.fees_microunits==floor_micro(best_ref.fees));
            } else assert(result.decision.reject==HotReject::NoPositiveEdge);
        } else if (result.search_exhausted) ++incomplete;
        else assert(!best_q); // validation/empty feasible domain is not profit
        if (result.decision.reject==HotReject::Accepted) {
            const auto ref=reference(t,books,resources,result.decision.quantity_microunits,10000);
            assert(ref.feasible && ref.net>0);
            assert(result.decision.net_pnl_microunits==floor_micro(ref.net));
        }
        assert(result.quantities_evaluated<=4096);
        if (result.net_upper_bound_valid) assert(Rat(result.net_upper_bound_microunits,1000000)>=best);
    }
    // Same-binary zero-fee, lattice-aligned depth: actual champion parity,
    // including reserves and zero/one/multiple-tick edge. Money differences
    // from floating conversion can only make the exact model more conservative.
    for (int trial=0;trial<3000;++trial) {
        CompiledRelation t; t.enabled=1;t.leg_count=2;t.guaranteed_payout_microunits=1000000;
        t.reserve_per_unit_microunits=500;
        for (unsigned i=0;i<2;++i) {
            t.legs[i].book_handle=i;t.legs[i].coefficient={1,1};t.legs[i].fee_verified=1;
            books[i]={};books[i].valid=books[i].lineage_continuous=1;books[i].ask_level_count=2;
            const int p=4900+random()%150;
            books[i].ask_levels[0]={p,10000*static_cast<std::int64_t>(1+random()%100)};
            books[i].ask_levels[1]={p+10,10000*static_cast<std::int64_t>(1+random()%100)};
        }
        auto result=evaluate_order_constrained_basket(t,books,{}, {},10000);
        same_sizing(result,evaluate_order_constrained_basket(t,books,{}, {},10000,512,workspace.get()));
        auto champion=pure_arb::sweep(books[0],books[1],0,1,.0005,true);
        assert(result.global_optimum_proven);
        if (result.decision.quantity_microunits!=champion.shares_microunits) ++champion_quantity_differences;
        assert(result.decision.quantity_microunits==champion.shares_microunits);
        assert(std::abs(result.decision.net_pnl_microunits-std::llround(champion.conservative_locked_pnl*1e6))<=1);
    }
    assert(proven>100 && incomplete==0 && champion_quantity_differences==0);
    std::size_t fee_model_differences=0, fee_quantity_differences=0;
    std::size_t floating_quantity_effects=0, greedy_quantity_effects=0, fragmentation_quantity_effects=0;
    std::size_t explained_differences=0;
    std::size_t floating_money_effects=0, reporting_rounding_effects=0, fragmentation_money_effects=0;
    std::size_t explained_money_differences=0;
    for (int trial=0;trial<3000;++trial) {
        CompiledRelation t;t.enabled=1;t.leg_count=2;t.guaranteed_payout_microunits=1000000;
        t.reserve_per_unit_microunits=500;
        const double rate=trial%2?.02:.07;
        for (unsigned i=0;i<2;++i) {
            t.legs[i].book_handle=i;t.legs[i].coefficient={1,1};t.legs[i].fee_verified=1;t.legs[i].fee_rate=rate;
            books[i]={};books[i].valid=books[i].lineage_continuous=1;books[i].ask_level_count=3;
            const int p=4600+random()%450;
            for (int j=0;j<3;++j) books[i].ask_levels[j]={p+j*10,10000*static_cast<std::int64_t>(1+random()%100)};
        }
        const auto result=evaluate_order_constrained_basket(t,books,{}, {},10000,4096);
        same_sizing(result,evaluate_order_constrained_basket(t,books,{}, {},10000,4096,workspace.get()));
        const auto champion=pure_arb::sweep(books[0],books[1],rate,1,.0005,true);
        assert(result.global_optimum_proven);
        const auto nanorate=static_cast<std::int64_t>(std::llround(rate*1e9));
        std::int64_t cap=INT64_MAX;
        for (unsigned i=0;i<2;++i) {
            std::int64_t total=0;
            for (unsigned j=0;j<books[i].ask_level_count;++j) total+=books[i].ask_levels[j].quantity_microunits;
            cap=std::min(cap,total);
        }
        const auto rational_greedy=basket_slices(books,nanorate,cap,true);
        SliceResult slice_optimum;
        // Exhaust the SAME order lattice, retaining smallest q on ties. This
        // isolates search from fee fragmentation and from floating arithmetic.
        for (std::int64_t q=10000;q<=cap;q+=10000) {
            const auto point=basket_slices(books,nanorate,q,false);
            assert(point.q==q);
            if (point.net>slice_optimum.net) slice_optimum=point;
        }
        const bool floating=champion.shares_microunits!=rational_greedy.q;
        const bool greedy=rational_greedy.q!=slice_optimum.q;
        const bool fragmentation=slice_optimum.q!=result.decision.quantity_microunits;
        floating_quantity_effects+=floating;greedy_quantity_effects+=greedy;
        fragmentation_quantity_effects+=fragmentation;
        const bool different=result.decision.quantity_microunits!=champion.shares_microunits;
        if (different) {
            ++fee_quantity_differences;
            assert(floating || greedy || fragmentation);
            ++explained_differences;
        }
        const auto same_q_sliced=basket_slices(books,nanorate,champion.shares_microunits,false);
        const auto same_q_per_level=reference(t,books,{},champion.shares_microunits,10000);
        assert(slice_optimum.net>=rational_greedy.net);
        assert(rational_greedy.net==basket_slices(books,nanorate,rational_greedy.q,false).net);
        const bool money_different=champion.shares_microunits>0 &&
            std::llround(champion.conservative_locked_pnl*1e6)!=floor_micro(same_q_per_level.net);
        const bool money_float=champion.shares_microunits>0 &&
            std::llround(champion.conservative_locked_pnl*1e6)!=round_positive_micro(same_q_sliced.net);
        const bool money_round=champion.shares_microunits>0 &&
            round_positive_micro(same_q_sliced.net)!=floor_micro(same_q_sliced.net);
        const bool money_fragment=champion.shares_microunits>0 &&
            floor_micro(same_q_sliced.net)!=floor_micro(same_q_per_level.net);
        floating_money_effects+=money_float;reporting_rounding_effects+=money_round;
        fragmentation_money_effects+=money_fragment;
        if (money_different) { assert(money_float || money_round || money_fragment); ++explained_money_differences; }
        if (argc==3 && (different || floating || greedy || fragmentation || money_different)) {
            json::array legs;
            for (unsigned i=0;i<2;++i) {
                json::array levels;
                for (unsigned j=0;j<books[i].ask_level_count;++j)
                    levels.push_back(json::array{books[i].ask_levels[j].price_e4,books[i].ask_levels[j].quantity_microunits});
                legs.push_back(std::move(levels));
            }
            attributions.push_back(json::object{{"trial",trial},{"ask_books_e4_microshares",std::move(legs)},
                {"fee_rate_nano",nanorate},{"champion_q",champion.shares_microunits},
                {"rational_shared_slice_greedy_q",rational_greedy.q},
                {"rational_shared_slice_optimal_q",slice_optimum.q},
                {"rational_per_leg_level_optimal_q",result.decision.quantity_microunits},
                {"floating_stopping_effect",floating},{"greedy_search_effect",greedy},
                {"fee_fragmentation_effect",fragmentation},{"end_to_end_quantity_difference",different},
                {"money_difference_at_champion_q",money_different},{"floating_money_effect",money_float},
                {"reporting_round_vs_floor_effect",money_round},{"money_fragmentation_effect",money_fragment},
                {"champion_net_rounded_micro",std::llround(champion.conservative_locked_pnl*1e6)},
                {"rational_shared_slice_net_at_champion_q",rational_text(same_q_sliced.net)},
                {"rational_per_leg_level_net_at_champion_q",rational_text(same_q_per_level.net)}});
        }
        if (champion.shares_microunits>0) {
            const auto at_champion=reference(t,books,{},champion.shares_microunits,10000);
            assert(at_champion.feasible);
            if (std::llround(champion.conservative_locked_pnl*1e6)!=floor_micro(at_champion.net)) ++fee_model_differences;
            // We compare both quantities under ONE rational accounting model,
            // not claim parity between distinct fee-fragmentation conventions.
            const auto at_selected=reference(t,books,{},result.decision.quantity_microunits,10000);
            assert(at_selected.net>=at_champion.net);
        }
    }
    // Deep ladders, partial tail and changing directions/fees reuse the SAME
    // storage. Compare every private monetary field against the uncached walk,
    // then every public decision/certificate under equal search budgets.
    for (unsigned trial=0;trial<10;++trial) {
        CompiledRelation t;t.enabled=1;t.leg_count=trial%5==4?16:2+trial%5;
        t.sell_inventory=trial%2;t.guaranteed_payout_microunits=1000000;t.reserve_per_unit_microunits=500;
        std::array<std::int64_t,kMaxLegs> inventory{};inventory.fill(3000000);
        HotResources resources;resources.inventory=inventory;resources.quantity_limit_microunits=1000000;
        for (unsigned i=0;i<t.leg_count;++i) {
            auto& leg=t.legs[i];leg.book_handle=i;leg.coefficient={1,1};leg.fee_verified=1;
            leg.fee_rate=.02;leg.fee_exponent=trial%3;
            auto& b=books[i];b={};b.valid=b.lineage_continuous=1;
            b.ask_level_count=b.bid_level_count=kDeepDepthLevels;
            for (unsigned j=0;j<kDeepDepthLevels;++j) {
                const auto quantity=1000+static_cast<std::int64_t>(random()%5000);
                b.ask_levels[j]={static_cast<int>(3000/t.leg_count+j),quantity};
                b.bid_levels[j]={static_cast<int>(7000/t.leg_count+1024-j),quantity};
            }
        }
        assert(order_sizing_detail::prepare_depth(t,books,1000000,*workspace));
        for (unsigned sample=0;sample<48;++sample) {
            const auto quantity=sample==0?1000000:10000*static_cast<std::int64_t>(1+random()%100);
            const auto a=order_sizing_detail::at_quantity(t,books,quantity);
            const auto b=order_sizing_detail::at_prepared_quantity(t,books,quantity,*workspace);
            assert(a.valid && b.valid && a.quantity==b.quantity && a.raw==b.raw && a.fees==b.fees
                && a.reserve==b.reserve && a.capital==b.capital && a.net==b.net && a.levels==b.levels);
        }
        for (unsigned budget:{0u,1u,2u,16u,512u})
            same_sizing(evaluate_order_constrained_basket(t,books,{},resources,10000,budget),
                        evaluate_order_constrained_basket(t,books,{},resources,10000,budget,workspace.get()));
    }
    {
        CompiledRelation huge;huge.enabled=1;huge.leg_count=2;huge.guaranteed_payout_microunits=1000000;
        for (unsigned i=0;i<2;++i) {
            huge.legs[i].book_handle=i;huge.legs[i].coefficient={1,1};huge.legs[i].fee_verified=1;
            huge.legs[i].fee_rate=1;huge.legs[i].fee_exponent=2;
            books[i]={};books[i].valid=books[i].lineage_continuous=1;
            books[i].ask_level_count=1;books[i].ask_levels[0]={4000,INT64_MAX};
        }
        const auto plain=evaluate_order_constrained_basket(huge,books,{}, {},10000);
        assert(plain.decision.reject==HotReject::NumericOverflow && plain.quantities_evaluated==2);
        same_sizing(plain,evaluate_order_constrained_basket(huge,books,{}, {},10000,512,workspace.get()));
        HotResources capped;capped.quantity_limit_microunits=20000;
        same_sizing(evaluate_order_constrained_basket(huge,books,{},capped,10000),
                    evaluate_order_constrained_basket(huge,books,{},capped,10000,512,workspace.get()));
    }
    assert(explained_differences==fee_quantity_differences);
    assert(explained_money_differences==fee_model_differences);
    if (argc==3) {
        // Do not overwrite an earlier research receipt.
        assert(!std::ifstream(argv[2]).good());
        json::object report{{"schema","polymarket_v7_binary_sizing_attribution_v1"},
            {"paper_only",true},{"authenticated_execution",false},{"real_order_submission",false},
            {"real_capital_at_risk",false},{"automatic_promotion",false},{"execution_authority",false},
            {"scope","SYNTHETIC_ACCOUNTING_COUNTERFACTUALS_NOT_VENUE_EXECUTION"},
            {"trials",3000},{"reserve_micro_per_share",500},{"order_quantum_microshares",10000},
            {"fee_exponent",1},{"quantity_differences",fee_quantity_differences},
            {"quantity_differences_explained",explained_differences},
            {"floating_stopping_effects",floating_quantity_effects},{"greedy_search_effects",greedy_quantity_effects},
            {"fee_fragmentation_effects",fragmentation_quantity_effects},
            {"money_differences",fee_model_differences},{"money_differences_explained",explained_money_differences},
            {"floating_money_effects",floating_money_effects},{"reporting_round_vs_floor_effects",reporting_rounding_effects},
            {"money_fragmentation_effects",fragmentation_money_effects},
            {"effects_are_overlapping_not_additive",true},{"venue_execution_verified",false},
            {"full_champion_parity",false},{"cases",std::move(attributions)}};
        std::ofstream file(argv[2]);file<<json::serialize(report)<<'\n';file.flush();assert(file.good());
    }
    std::cout << "rational_oracle_trials=3000 proven="<<proven<<" incomplete="<<incomplete
              <<" zero_fee_champion_trials=3000 quantity_differences="<<champion_quantity_differences
              <<" nonzero_fee_champion_trials=3000 quantity_differences="<<fee_quantity_differences
              <<" money_model_differences="<<fee_model_differences
              <<" floating_stopping_effects="<<floating_quantity_effects
              <<" greedy_search_effects="<<greedy_quantity_effects
              <<" fee_fragmentation_effects="<<fragmentation_quantity_effects
              <<" quantity_differences_explained="<<explained_differences<<'\n';
}
