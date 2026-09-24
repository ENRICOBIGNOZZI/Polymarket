#pragma once
#include "pm/v7_exact_arb_graph_hotpath.hpp"

namespace pm::v7::exact_arb_graph {

struct OrderSizing {
    HotDecision decision{};
    std::int64_t unconstrained_quantity_microunits = 0;
    std::int64_t relation_quantum_microunits = 0;
    std::uint8_t quantity_precision_ready = 0;
    // A proof is only about the recorded PER_L2_LEVEL_5DP model, not exchange
    // fragmentation, fills, or settlement. The venue remains unverified.
    std::uint8_t global_optimum_proven = 0;
    std::uint8_t search_exhausted = 0;
    std::uint32_t quantities_evaluated = 0;
    std::int64_t net_upper_bound_microunits = 0;
    std::uint8_t net_upper_bound_valid = 0;
};

// Find the smallest relation quantity for which EVERY coefficient*q is an
// integer multiple of the venue's total-order share quantum. Fill fragments
// and individual L2 levels do not have to satisfy that order-size quantum.
[[nodiscard]] inline std::int64_t order_quantity_quantum(
    const CompiledRelation& relation, std::int64_t shares_quantum) noexcept {
    if (shares_quantum <= 0 || relation.leg_count == 0 || relation.leg_count > kMaxLegs) return 0;
    std::int64_t quantum = 1;
    for (std::size_t i = 0; i < relation.leg_count; ++i) {
        const auto& c = relation.legs[i].coefficient;
        if (!c.valid()) return 0;
        // Reduce first, then use gcd cancellation to avoid rejecting a valid
        // representable quantum merely because denominator*precision overflows.
        const auto common = std::gcd(c.numerator, c.denominator);
        const auto n = c.numerator/common, d = c.denominator/common;
        const auto factor = shares_quantum/std::gcd(n, shares_quantum);
        if (d > INT64_MAX/factor) return 0;
        const auto leg_quantum = d*factor;
        const auto multiplier = leg_quantum/std::gcd(quantum, leg_quantum);
        if (quantum > INT64_MAX/multiplier) return 0;
        quantum *= multiplier;
    }
    return quantum;
}

namespace order_sizing_detail {
using Wide = __int128;
inline constexpr Wide kMoneyScale = 1'000'000; // pico-PUSD -> micro-PUSD

struct Point {
    std::int64_t quantity = 0;
    Wide raw = 0, fees = 0, reserve = 0, capital = 0, net = 0;
    std::array<std::uint16_t, kMaxLegs> levels{};
    bool valid = false;
};

// Caller-owned scratch, allocated once off path by NativeGraphRuntime. Prefix
// fees use the SAME per-L2 rounding as at_quantity, never a different model.
// Rebuilt for each relation invocation; no stale book/fee cache can be reused.
struct DepthWorkspace {
    struct Prefix { Wide quantity=0, cash=0, fees=0; };
    std::array<std::array<Prefix,kDeepDepthLevels+1>,kMaxLegs> prefix{};
    std::array<std::uint16_t,kMaxLegs> counts{};
};

[[nodiscard]] inline Point finish_point(const CompiledRelation& r,Point p,Wide cash) noexcept {
    const Wide payout=static_cast<Wide>(p.quantity)*r.guaranteed_payout_microunits;
    p.raw=r.sell_inventory?cash-payout:payout-cash;
    p.reserve=static_cast<Wide>(p.quantity)*r.reserve_per_unit_microunits;
    if (__builtin_sub_overflow(p.raw,p.reserve,&p.net)
        || __builtin_sub_overflow(p.net,p.fees,&p.net)) return p;
    p.capital=r.sell_inventory?0:cash;
    if ((!r.sell_inventory && __builtin_add_overflow(p.capital,p.fees,&p.capital))
        || __builtin_add_overflow(p.capital,p.reserve,&p.capital)) return p;
    p.valid=true;return p;
}

// Exact cumulative order cost. L2 fragments may be smaller than the order lot;
// no liquidity is skipped and another leg's breakpoints never split a fee.
// All monetary values here are pico-PUSD (1e-12), not floating point.
[[nodiscard]] inline Point at_quantity(const CompiledRelation& r,
    std::span<const BookDeepSnapshot> books, std::int64_t q) noexcept {
    Point p; p.quantity = q;
    Wide cash = 0;
    for (std::size_t i = 0; i < r.leg_count; ++i) {
        const auto& leg = r.legs[i];
        const Wide scaled = static_cast<Wide>(q)*leg.coefficient.numerator;
        if (scaled % leg.coefficient.denominator) return p;
        const Wide shares = scaled/leg.coefficient.denominator;
        if (shares > INT64_MAX) return p;
        auto left = static_cast<std::int64_t>(shares);
        const auto& b = books[leg.book_handle];
        const auto count = r.sell_inventory ? b.bid_level_count : b.ask_level_count;
        const auto& levels = r.sell_inventory ? b.bid_levels : b.ask_levels;
        for (std::size_t j = 0; left > 0 && j < count; ++j) {
            const auto take = std::min(left, levels[j].quantity_microunits);
            cash += static_cast<Wide>(take)*levels[j].price_e4*100;
            const auto fee = rounded_fee_units(take, levels[j].price_e4, leg);
            constexpr auto maximum = (~static_cast<unsigned __int128>(0)) >> 1;
            if (!fee.valid || fee.units > maximum/10'000'000) return p;
            if (__builtin_add_overflow(p.fees, static_cast<Wide>(fee.units)*10'000'000, &p.fees)) return p;
            p.levels[i] = static_cast<std::uint16_t>(j+1);
            left -= take;
        }
        if (left) return p;
    }
    return finish_point(r,p,cash);
}

// Preconditions: relation, fee terms and books have passed the sizing entry
// checks. Build only the depth reachable at max_q, including its partial tail;
// unreachable sizes cannot cause a new overflow rejection or extra fee debit.
[[nodiscard]] inline bool prepare_depth(const CompiledRelation& r,
    std::span<const BookDeepSnapshot> books,std::int64_t max_q,DepthWorkspace& workspace) noexcept {
    for (std::size_t i=0;i<r.leg_count;++i) {
        const auto& leg=r.legs[i];const auto& b=books[leg.book_handle];
        const Wide scaled=static_cast<Wide>(max_q)*leg.coefficient.numerator;
        if (scaled%leg.coefficient.denominator) return false;
        Wide left=scaled/leg.coefficient.denominator;
        if (left<=0 || left>INT64_MAX) return false;
        const auto& levels=r.sell_inventory?b.bid_levels:b.ask_levels;
        const auto count=r.sell_inventory?b.bid_level_count:b.ask_level_count;
        auto& prefix=workspace.prefix[i];prefix[0]={};workspace.counts[i]=0;
        for (std::size_t j=0;left>0 && j<count;++j) {
            const auto take=static_cast<std::int64_t>(std::min<Wide>(left,levels[j].quantity_microunits));
            const auto fee=rounded_fee_units(take,levels[j].price_e4,leg);
            constexpr auto maximum=(~static_cast<unsigned __int128>(0))>>1;
            if (!fee.valid || fee.units>maximum/10'000'000) return false;
            auto next=prefix[j];
            next.quantity+=take;next.cash+=static_cast<Wide>(take)*levels[j].price_e4*100;
            if (__builtin_add_overflow(next.fees,static_cast<Wide>(fee.units)*10'000'000,&next.fees)) return false;
            prefix[j+1]=next;workspace.counts[i]=static_cast<std::uint16_t>(j+1);left-=take;
        }
        if (left) return false;
    }
    return true;
}

[[nodiscard]] inline Point at_prepared_quantity(const CompiledRelation& r,
    std::span<const BookDeepSnapshot> books,std::int64_t q,const DepthWorkspace& workspace) noexcept {
    Point p;p.quantity=q;Wide cash=0;
    for (std::size_t i=0;i<r.leg_count;++i) {
        const auto& leg=r.legs[i];const Wide scaled=static_cast<Wide>(q)*leg.coefficient.numerator;
        if (scaled%leg.coefficient.denominator) return p;
        const Wide shares=scaled/leg.coefficient.denominator;
        if (shares<=0 || shares>INT64_MAX) return p;
        const auto& prefix=workspace.prefix[i];const auto count=workspace.counts[i];
        const auto end=prefix.begin()+count+1;
        const auto it=std::lower_bound(prefix.begin()+1,end,shares,
            [](const auto& level,Wide required) { return level.quantity<required; });
        if (it==end) return p;
        const auto index=static_cast<std::size_t>(it-prefix.begin());
        Wide leg_cash=0,leg_fees=0;
        if (it->quantity==shares) { leg_cash=it->cash;leg_fees=it->fees; }
        else {
            const auto& previous=prefix[index-1];
            const auto take=static_cast<std::int64_t>(shares-previous.quantity);
            const auto& b=books[leg.book_handle];const auto& levels=r.sell_inventory?b.bid_levels:b.ask_levels;
            const auto fee=rounded_fee_units(take,levels[index-1].price_e4,leg);
            constexpr auto maximum=(~static_cast<unsigned __int128>(0))>>1;
            if (!fee.valid || fee.units>maximum/10'000'000) return p;
            leg_cash=previous.cash+static_cast<Wide>(take)*levels[index-1].price_e4*100;
            if (__builtin_add_overflow(previous.fees,static_cast<Wide>(fee.units)*10'000'000,&leg_fees)) return p;
        }
        cash+=leg_cash;
        if (__builtin_add_overflow(p.fees,leg_fees,&p.fees)) return p;
        p.levels[i]=static_cast<std::uint16_t>(index);
    }
    return finish_point(r,p,cash);
}

[[nodiscard]] inline bool micro_money(Wide value, bool ceil, std::int64_t& out) noexcept {
    Wide result = value/kMoneyScale;
    if (value%kMoneyScale) result += ceil ? (value > 0 ? 1 : 0) : (value < 0 ? -1 : 0);
    if (result < INT64_MIN || result > INT64_MAX) return false;
    out = static_cast<std::int64_t>(result); return true;
}
} // namespace order_sizing_detail

// Bounded exact branch-and-bound on the venue order lattice. Raw-minus-reserve
// is concave on sorted full-depth books; cumulative rounded fees are monotone.
// Thus max(raw-reserve on [lo,hi]) - fee(lo) is an admissible upper bound even
// across fee discontinuities. Exhaustion keeps a feasible incumbent, NEVER a
// false optimality/no-opportunity certificate. Stack/work do not depend on q.
[[nodiscard]] inline OrderSizing evaluate_order_constrained_basket(
    const CompiledRelation& r, std::span<const BookDeepSnapshot> books,
    HotTimingContext timing, HotResources resources, std::int64_t shares_quantum,
    std::uint32_t evaluation_budget = 512,order_sizing_detail::DepthWorkspace* workspace = nullptr) noexcept {
    using namespace order_sizing_detail;
    OrderSizing out; out.decision.relation_handle = r.relation_handle;
    evaluation_budget = std::min<std::uint32_t>(evaluation_budget, 4096);
    auto reject = [&](HotReject why) { out.decision.reject = why; return out; };
    if (!r.enabled || !r.leg_count || r.leg_count > kMaxLegs || r.sell_inventory > 1
        || r.guaranteed_payout_microunits <= 0 || r.reserve_per_unit_microunits < 0
        || resources.quantity_limit_microunits < 0) return reject(HotReject::InvalidRelation);
    const auto quantum = order_quantity_quantum(r, shares_quantum);
    out.relation_quantum_microunits = quantum;
    if (!quantum) return reject(HotReject::VenuePrecision);
    if (resources.capital_microunits < 0) return reject(HotReject::CapitalLimit);
    auto cap = resources.quantity_limit_microunits;
    std::int64_t minimum = 1, earliest = INT64_MAX, latest = 0;
    if (resources.transformation) {
        const auto& t = *resources.transformation;
        if (!t.verified || t.capacity_microunits <= 0 || t.latency_ns < 0 || t.capital_lock_ns <= 0)
            return reject(HotReject::TransformationUnavailable);
        cap = std::min(cap, t.capacity_microunits);
    }
    for (std::size_t i = 0; i < r.leg_count; ++i) {
        const auto& leg = r.legs[i];
        if (leg.minimum_order_microunits < 0) return reject(HotReject::InvalidRelation);
        if (leg.book_handle >= books.size()) return reject(HotReject::MissingBook);
        if (!valid_fee_terms(leg)) return reject(HotReject::UnknownFee);
        for (std::size_t j = 0; j < i; ++j)
            if (r.legs[j].book_handle == leg.book_handle) return reject(HotReject::InvalidRelation);
        const auto& b = books[leg.book_handle];
        if (!valid_book(b, !r.sell_inventory)) return reject(HotReject::IncompleteDepth);
        const auto stamp = b.receive_monotonic_ns;
        if ((timing.maximum_book_age_ns > 0 || timing.maximum_leg_skew_ns > 0)
            && (stamp <= 0 || timing.now_receive_monotonic_ns <= 0 || stamp > timing.now_receive_monotonic_ns
                || (timing.maximum_book_age_ns > 0
                    && timing.now_receive_monotonic_ns-stamp > timing.maximum_book_age_ns)))
            return reject(HotReject::StaleBook);
        if (stamp > 0) { earliest = std::min(earliest, stamp); latest = std::max(latest, stamp); }
        const auto count = r.sell_inventory ? b.bid_level_count : b.ask_level_count;
        const auto& levels = r.sell_inventory ? b.bid_levels : b.ask_levels;
        Wide depth = 0;
        for (std::size_t j = 0; j < count; ++j) depth += levels[j].quantity_microunits;
        // Each leg's submitted quantity must fit the signed native share type.
        depth = std::min<Wide>(depth, INT64_MAX);
        if (r.sell_inventory) {
            if (leg.book_handle >= resources.inventory.size() || resources.inventory[leg.book_handle] <= 0)
                return reject(HotReject::InventoryUnavailable);
            depth = std::min<Wide>(depth, resources.inventory[leg.book_handle]);
        }
        cap = static_cast<std::int64_t>(std::min<Wide>(cap,
            depth*leg.coefficient.denominator/leg.coefficient.numerator));
        const Wide needed = (static_cast<Wide>(leg.minimum_order_microunits)*leg.coefficient.denominator
            + leg.coefficient.numerator-1)/leg.coefficient.numerator;
        if (needed > INT64_MAX) return reject(HotReject::NumericOverflow);
        minimum = std::max(minimum, static_cast<std::int64_t>(needed));
    }
    if (timing.maximum_leg_skew_ns > 0 && latest-earliest > timing.maximum_leg_skew_ns)
        return reject(HotReject::LegSkew);
    const auto first = minimum/quantum + (minimum%quantum != 0);
    auto last = cap/quantum;
    if (first > last) return reject(cap < minimum ? HotReject::MinimumOrder : HotReject::VenuePrecision);

    // Retain the old sweep's q as a diagnostic comparison, never its money or
    // stopping decision. This field is not the optimum of a continuous problem.
    out.unconstrained_quantity_microunits = evaluate_basket(r, books, !r.sell_inventory, timing, resources).quantity_microunits;
    Point best; bool numeric_error = false, prepared=false;
    const Wide budget_money = static_cast<Wide>(resources.capital_microunits)*kMoneyScale;
    auto point = [&](std::int64_t lot) {
        if (out.quantities_evaluated >= evaluation_budget) { out.search_exhausted = 1; return Point{}; }
        ++out.quantities_evaluated;
        auto p = prepared?at_prepared_quantity(r,books,lot*quantum,*workspace):at_quantity(r,books,lot*quantum);
        if (!p.valid) numeric_error = true;
        if (p.valid && p.capital <= budget_money && p.net > 0
            && (!best.valid || p.net > best.net || (p.net == best.net && p.quantity < best.quantity))) best = p;
        return p;
    };
    auto finish = [&]() {
        if (numeric_error) { out.global_optimum_proven = 0; out.net_upper_bound_valid = 0;
            out.decision = {}; out.decision.relation_handle = r.relation_handle; return reject(HotReject::NumericOverflow); }
        if (!best.valid) return reject(out.search_exhausted ? HotReject::SizingIncomplete : HotReject::NoPositiveEdge);
        auto& d = out.decision;
        if (!micro_money(best.raw, false, d.raw_pnl_microunits)
            || !micro_money(best.raw-best.fees, false, d.gross_pnl_microunits)
            || !micro_money(best.net, false, d.net_pnl_microunits)
            || !micro_money(best.fees, true, d.fees_microunits)
            || !micro_money(best.capital, true, d.capital_required_microunits)) {
            out.global_optimum_proven = 0; return reject(HotReject::NumericOverflow);
        }
        d.quantity_microunits = best.quantity; d.levels_consumed = best.levels;
        for (auto n : best.levels) d.levels_used += n;
        out.quantity_precision_ready = 1;
        return reject(d.net_pnl_microunits > 0 ? HotReject::Accepted : HotReject::NoPositiveEdge);
    };
    const auto low_point = point(first);
    if (!low_point.valid) return finish();
    if (low_point.capital > budget_money) return reject(HotReject::CapitalLimit);
    // f(0)=0 and f=raw-reserve is concave. If even the smallest admissible
    // order has non-positive f, larger orders cannot become profitable by
    // adding nonnegative fees. This is a proof, not a fee-based greedy cutoff.
    if (low_point.raw-low_point.reserve <= 0) {
        out.global_optimum_proven = 1; out.net_upper_bound_valid = 1;
        return finish();
    }
    if (workspace && last>first && out.quantities_evaluated<evaluation_budget) {
        // A prefix overflow must not change which quantity is evaluated or
        // when the original walker reports failure. Fall back to that walker,
        // preserving its exact budget/certificate and rejection semantics.
        prepared=prepare_depth(r,books,last*quantum,*workspace);
    }
    auto high_point = first == last ? low_point : point(last);
    if (!high_point.valid) return finish();
    if (high_point.capital > budget_money) {
        auto lo = first, hi = last;
        while (lo < hi) {
            const auto mid = lo+(hi-lo)/2+(hi-lo)%2;
            const auto p = point(mid); if (!p.valid) return finish();
            if (p.capital <= budget_money) lo = mid; else hi = mid-1;
        }
        last = lo;
        high_point = point(last); if (!high_point.valid) return finish();
    }
    if (last == first) {
        out.global_optimum_proven = 1;
        out.net_upper_bound_valid = micro_money(best.valid ? best.net : 0, true, out.net_upper_bound_microunits);
        return finish();
    }
    // Certify strictly increasing net PnL without searching every lot. The
    // smallest raw-reserve increment is the last one (concavity). A fee's
    // rounding error lies in (-1,+1/2] fee units, including the sub-unit waiver,
    // so its increase is <= raw fee increase + 1.5 units per touched L2 level.
    // p(1-p)<=1/4 bounds the unrounded fee across ALL intervening prices. This
    // deliberately loose integer bound is sufficient for wide-edge baskets.
    const auto previous_point = point(last-1); if (!previous_point.valid) return finish();
    Wide fee_increment_bound = 0;
    for (std::size_t i=0; i<r.leg_count; ++i) {
        const auto& leg = r.legs[i];
        if (leg.fee_rate == 0) continue;
        const Wide leg_step = static_cast<Wide>(quantum)*leg.coefficient.numerator/leg.coefficient.denominator;
        const auto rate = static_cast<std::int64_t>(std::llround(leg.fee_rate*1e9));
        const std::int64_t denominator = 1000*(static_cast<int>(leg.fee_exponent)==0 ? 1
            : static_cast<int>(leg.fee_exponent)==1 ? 4 : 16);
        const Wide numerator = leg_step*rate;
        const auto& b = books[leg.book_handle];
        const auto count = r.sell_inventory ? b.bid_level_count : b.ask_level_count;
        fee_increment_bound += (numerator+denominator-1)/denominator + static_cast<Wide>(count)*15'000'000;
    }
    if ((high_point.raw-high_point.reserve)-(previous_point.raw-previous_point.reserve) > fee_increment_bound) {
        out.global_optimum_proven = 1;
        out.net_upper_bound_valid = micro_money(best.valid ? best.net : 0, true, out.net_upper_bound_microunits);
        return finish();
    }
    // Leftmost maximum of the discrete concave raw-minus-reserve function.
    auto lo = first, hi = last;
    while (lo < hi) {
        const auto mid = lo+(hi-lo)/2;
        const auto a = point(mid), b = point(mid+1);
        if (!a.valid || !b.valid) return finish();
        if (a.raw-a.reserve < b.raw-b.reserve) lo = mid+1; else hi = mid;
    }
    const auto peak = lo;
    const auto peak_point = point(peak); if (!peak_point.valid) return finish();
    bool zero_fees = true;
    for (std::size_t i = 0; i < r.leg_count; ++i) zero_fees &= r.legs[i].fee_rate == 0;
    if (zero_fees) {
        out.global_optimum_proven = 1;
        out.net_upper_bound_valid = micro_money(best.valid ? best.net : 0, true, out.net_upper_bound_microunits);
        return finish();
    }
    const Wide initial_bound = std::max<Wide>(0, peak_point.raw-peak_point.reserve-low_point.fees);
    out.net_upper_bound_valid = micro_money(initial_bound, true, out.net_upper_bound_microunits);
    struct Interval { std::int64_t low, high; };
    std::array<Interval, 64> stack{};
    std::size_t pending = 1; stack[0] = {first, last};
    while (pending) {
        const auto [a,b] = stack[--pending];
        const auto left = point(a);
        if (!left.valid) return finish();
        const auto top_index = std::clamp(peak, a, b);
        const auto top = top_index == a ? left : point(top_index);
        if (!top.valid) return finish();
        const Wide bound = top.raw-top.reserve-left.fees;
        const Wide incumbent = best.valid ? best.net : 0;
        if (bound < incumbent || (bound == incumbent && (!best.valid || a*quantum >= best.quantity)) || a == b) continue;
        const auto mid = a+(b-a)/2;
        // Depth-first bisection of a signed-64-bit positive lattice needs <=63
        // pending siblings. Keep a guard even though the bound is structural.
        if (pending+2 > stack.size()) { out.search_exhausted = 1; return finish(); }
        stack[pending++] = {mid+1,b}; stack[pending++] = {a,mid};
    }
    out.global_optimum_proven = 1;
    out.net_upper_bound_valid = micro_money(best.valid ? best.net : 0, true, out.net_upper_bound_microunits);
    return finish();
}
} // namespace pm::v7::exact_arb_graph
