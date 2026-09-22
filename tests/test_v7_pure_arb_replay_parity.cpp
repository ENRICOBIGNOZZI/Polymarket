#include "pm/v7_pure_arb_lane.hpp"

#include <algorithm>
#include <bit>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <limits>

using namespace pm::v7;
using namespace pm::v7::pure_arb;

namespace {

struct RefSweep {
    std::int64_t shares_microunits = 0;
    double gross_locked_pnl = 0.0;
    double conservative_locked_pnl = 0.0;
    double yes_notional = 0.0;
    double no_notional = 0.0;
    double marginal_edge_per_share = 0.0;
    std::uint16_t yes_levels_used = 0;
    std::uint16_t no_levels_used = 0;
    std::int32_t yes_limit_e4 = 0;
    std::int32_t no_limit_e4 = 0;
};

double ref_price(std::int32_t e4) noexcept {
    return static_cast<double>(e4) / 10'000.0;
}

double ref_fee_per_share(double p, double rate, double exponent) noexcept {
    if (!std::isfinite(p) || p <= 0.0 || p >= 1.0
        || !std::isfinite(rate) || rate < 0.0 || rate > 1.0
        || !std::isfinite(exponent) || exponent < 0.0) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    return rate == 0.0 ? 0.0
                       : rate * std::pow(p * (1.0 - p), exponent);
}

double ref_fee_usdc(double shares, double p, double rate, double exponent) noexcept {
    const double per_share = ref_fee_per_share(p, rate, exponent);
    if (!std::isfinite(shares) || shares <= 0.0 || !std::isfinite(per_share))
        return std::numeric_limits<double>::quiet_NaN();
    const double raw = shares * per_share;
    if (raw < 0.00001 - 1e-15) return 0.0;
    return std::round(raw * 100000.0) / 100000.0;
}

template <std::size_t N>
RefSweep ref_sweep_levels(
    const std::array<PriceLevelE4,N>& yes_levels, std::size_t yes_count,
    const std::array<PriceLevelE4,N>& no_levels, std::size_t no_count,
    double fee_rate, double fee_exponent, double reserve,
    bool buy, std::int64_t max_quantity) noexcept {
    RefSweep out{};
    if (!std::isfinite(reserve) || reserve < 0.0 || max_quantity <= 0) return out;
    std::size_t yi=0, ni=0;
    std::int64_t yr=0, nr=0;
    std::int64_t cap=std::max<std::int64_t>(0,max_quantity);
    while(yi<yes_count && ni<no_count && cap>0) {
        if(yr<=0) yr=yes_levels[yi].quantity_microunits;
        if(nr<=0) nr=no_levels[ni].quantity_microunits;
        if(yr<=0){++yi; continue;}
        if(nr<=0){++ni; continue;}
        const double yp=ref_price(yes_levels[yi].price_e4);
        const double np=ref_price(no_levels[ni].price_e4);
        const auto q=std::min({yr,nr,cap});
        if(q<=0) break;
        const double shares=static_cast<double>(q)/1'000'000.0;
        const double fee_total=
            ref_fee_usdc(shares,yp,fee_rate,fee_exponent)
            + ref_fee_usdc(shares,np,fee_rate,fee_exponent);
        if(!std::isfinite(fee_total)) break;
        const double edge=buy ? 1.0-yp-np-fee_total/shares
                              : yp+np-1.0-fee_total/shares;
        if(!(edge>reserve+1e-12)) break;
        out.shares_microunits+=q;
        out.gross_locked_pnl+=shares*edge;
        out.conservative_locked_pnl+=shares*(edge-reserve);
        out.yes_notional+=shares*yp;
        out.no_notional+=shares*np;
        out.marginal_edge_per_share=edge;
        out.yes_limit_e4=yes_levels[yi].price_e4;
        out.no_limit_e4=no_levels[ni].price_e4;
        out.yes_levels_used=static_cast<std::uint16_t>(
            std::max<std::size_t>(out.yes_levels_used,yi+1));
        out.no_levels_used=static_cast<std::uint16_t>(
            std::max<std::size_t>(out.no_levels_used,ni+1));
        yr-=q; nr-=q; cap-=q;
        if(yr<=0) ++yi;
        if(nr<=0) ++ni;
    }
    return out;
}

RefSweep ref_sweep(const BookHotSnapshot& y,const BookHotSnapshot& n,
                   double fr,double fe,double reserve,bool buy,
                   std::int64_t maxq=std::numeric_limits<std::int64_t>::max()) noexcept {
    return buy
        ? ref_sweep_levels(y.ask_levels,y.ask_level_count,n.ask_levels,n.ask_level_count,
                           fr,fe,reserve,true,maxq)
        : ref_sweep_levels(y.bid_levels,y.bid_level_count,n.bid_levels,n.bid_level_count,
                           fr,fe,reserve,false,maxq);
}

bool ref_executable(const BookHotSnapshot& b) noexcept {
    return b.valid!=0 && b.lineage_continuous!=0
        && b.tick_size_e4>0 && b.tick_size_e4<10'000
        && b.best_bid_e4>0 && b.best_ask_e4>b.best_bid_e4
        && b.best_ask_e4<10'000
        && b.best_bid_microunits>0 && b.best_ask_microunits>0
        && b.bid_level_count>0 && b.ask_level_count>0;
}

struct ReferencePlan {
    DecisionReason reason=DecisionReason::InvalidInput;
    Direction direction=Direction::None;
    RefSweep economics{},buy{},sell{};
    std::uint64_t market_handle=0,event_handle=0;
    std::uint64_t yes_instrument=0,no_instrument=0;
    std::uint64_t yes_version=0,no_version=0;
    std::int64_t quantity=0;
    std::int32_t yes_limit=0,no_limit=0;
    Side side=Side::None;
    double buy_raw=0,sell_raw=0,buy_fee=0,sell_fee=0,buy_edge=0,sell_edge=0;
    double buy_l1=0,sell_l1=0;
    std::uint8_t accepted=0;
};

ReferencePlan reference_evaluate(const PairInput& in) noexcept {
    ReferencePlan out{};
    out.market_handle=in.market_handle; out.event_handle=in.event_handle;
    if(in.market_handle==0 || in.yes_instrument_handle==0 || in.no_instrument_handle==0
       || in.minimum_order_microunits<=0 || in.maximum_leg_skew_ns<=0
       || in.trigger_receive_monotonic_ns<=0
       || (in.decode_complete_monotonic_ns!=0
           && in.decode_complete_monotonic_ns<in.trigger_receive_monotonic_ns)
       || (in.decision_monotonic_ns!=0
           && in.decision_monotonic_ns<in.trigger_receive_monotonic_ns)
       || !std::isfinite(in.reserve_per_share) || in.reserve_per_share<0.0)
        return out;
    if(!(in.market_start_wall_ms<=in.now_wall_ms && in.now_wall_ms<in.market_end_wall_ms)){
        out.reason=DecisionReason::OutsideMarketWindow; return out;
    }
    if(in.fee_verified==0){out.reason=DecisionReason::FeeUnverified;return out;}
    if(in.yes_epoch==0 || in.yes_epoch!=in.no_epoch){out.reason=DecisionReason::EpochMismatch;return out;}
    if(in.yes.receive_monotonic_ns<=0 || in.no.receive_monotonic_ns<=0) return out;
    const auto skew=in.yes.receive_monotonic_ns>=in.no.receive_monotonic_ns
        ? in.yes.receive_monotonic_ns-in.no.receive_monotonic_ns
        : in.no.receive_monotonic_ns-in.yes.receive_monotonic_ns;
    if(skew>in.maximum_leg_skew_ns){out.reason=DecisionReason::LegSkewExceeded;return out;}
    if(in.yes.lineage_continuous==0 || in.no.lineage_continuous==0){
        out.reason=DecisionReason::LineageInvalid;return out;
    }
    if(!ref_executable(in.yes)||!ref_executable(in.no)){
        out.reason=DecisionReason::BookInvalid;return out;
    }
    const double ya=ref_price(in.yes.best_ask_e4), na=ref_price(in.no.best_ask_e4);
    const double yb=ref_price(in.yes.best_bid_e4), nb=ref_price(in.no.best_bid_e4);
    out.buy_l1=static_cast<double>(std::min(in.yes.best_ask_microunits,in.no.best_ask_microunits))/1'000'000.0;
    out.sell_l1=static_cast<double>(std::min(in.yes.best_bid_microunits,in.no.best_bid_microunits))/1'000'000.0;
    out.buy_fee=(ref_fee_usdc(out.buy_l1,ya,in.fee_rate,in.fee_exponent)
                +ref_fee_usdc(out.buy_l1,na,in.fee_rate,in.fee_exponent))/out.buy_l1;
    out.sell_fee=(ref_fee_usdc(out.sell_l1,yb,in.fee_rate,in.fee_exponent)
                 +ref_fee_usdc(out.sell_l1,nb,in.fee_rate,in.fee_exponent))/out.sell_l1;
    if(!std::isfinite(out.buy_fee)||!std::isfinite(out.sell_fee)){
        out.reason=DecisionReason::FeeInvalid;return out;
    }
    out.buy_raw=1.0-ya-na; out.sell_raw=yb+nb-1.0;
    out.buy_edge=out.buy_raw-out.buy_fee; out.sell_edge=out.sell_raw-out.sell_fee;
    out.buy=ref_sweep(in.yes,in.no,in.fee_rate,in.fee_exponent,in.reserve_per_share,true);
    out.sell=ref_sweep(in.yes,in.no,in.fee_rate,in.fee_exponent,in.reserve_per_share,false,
                       std::max<std::int64_t>(0,in.sell_available_microunits));
    const RefSweep* selected=nullptr;
    if(out.buy.shares_microunits>0){selected=&out.buy;out.direction=Direction::BuyCompleteSet;out.side=Side::Buy;}
    else if(out.sell.shares_microunits>0){selected=&out.sell;out.direction=Direction::SellCompleteSet;out.side=Side::Sell;}
    else {
        out.reason=in.sell_available_microunits<in.minimum_order_microunits
            ? DecisionReason::SellInventoryUnavailable : DecisionReason::NoPositiveEdge;
        return out;
    }
    if(selected->shares_microunits<in.minimum_order_microunits){
        out.reason=DecisionReason::BelowVenueMinimum;return out;
    }
    if(out.direction==Direction::SellCompleteSet
       && in.sell_available_microunits<selected->shares_microunits){
        out.reason=DecisionReason::SellInventoryUnavailable;return out;
    }
    out.reason=DecisionReason::Accepted; out.accepted=1; out.economics=*selected;
    out.yes_instrument=in.yes_instrument_handle; out.no_instrument=in.no_instrument_handle;
    out.yes_version=in.yes.state_version; out.no_version=in.no.state_version;
    out.quantity=selected->shares_microunits;
    out.yes_limit=selected->yes_limit_e4; out.no_limit=selected->no_limit_e4;
    return out;
}

std::uint64_t bits(double x) noexcept { return std::bit_cast<std::uint64_t>(x); }

void same_sweep(const SweepResult& a,const RefSweep& b){
    assert(a.shares_microunits==b.shares_microunits);
    assert(bits(a.gross_locked_pnl)==bits(b.gross_locked_pnl));
    assert(bits(a.conservative_locked_pnl)==bits(b.conservative_locked_pnl));
    assert(bits(a.yes_notional)==bits(b.yes_notional));
    assert(bits(a.no_notional)==bits(b.no_notional));
    assert(bits(a.marginal_edge_per_share)==bits(b.marginal_edge_per_share));
    assert(a.yes_levels_used==b.yes_levels_used);
    assert(a.no_levels_used==b.no_levels_used);
    assert(a.yes_limit_e4==b.yes_limit_e4);
    assert(a.no_limit_e4==b.no_limit_e4);
}

void same_plan(const PureArbExecutionPlan& a,const ReferencePlan& b){
    assert(a.reason==b.reason); assert(a.direction==b.direction); assert(a.accepted==b.accepted);
    assert(a.market_handle==b.market_handle); assert(a.event_handle==b.event_handle);
    same_sweep(a.economics,b.economics); same_sweep(a.buy_economics,b.buy); same_sweep(a.sell_economics,b.sell);
    assert(bits(a.buy_raw_edge_per_share)==bits(b.buy_raw));
    assert(bits(a.sell_raw_edge_per_share)==bits(b.sell_raw));
    assert(bits(a.buy_fee_per_share)==bits(b.buy_fee));
    assert(bits(a.sell_fee_per_share)==bits(b.sell_fee));
    assert(bits(a.buy_edge_per_share)==bits(b.buy_edge));
    assert(bits(a.sell_edge_per_share)==bits(b.sell_edge));
    assert(bits(a.buy_l1_shares)==bits(b.buy_l1));
    assert(bits(a.sell_l1_shares)==bits(b.sell_l1));
    if(a.accepted){
        assert(a.yes.instrument_handle==b.yes_instrument);
        assert(a.no.instrument_handle==b.no_instrument);
        assert(a.yes.market_state_version==b.yes_version);
        assert(a.no.market_state_version==b.no_version);
        assert(a.yes.quantity_microunits==b.quantity);
        assert(a.no.quantity_microunits==b.quantity);
        assert(a.yes.limit_price_e4==b.yes_limit);
        assert(a.no.limit_price_e4==b.no_limit);
        assert(a.yes.side==b.side && a.no.side==b.side);
    }
}

PairInput base_input(){
    PairInput x{};
    x.market_handle=7; x.event_handle=8;
    x.yes_instrument_handle=11; x.no_instrument_handle=12;
    x.yes_epoch=x.no_epoch=3;
    x.market_start_wall_ms=1'000; x.market_end_wall_ms=2'000; x.now_wall_ms=1'500;
    x.trigger_receive_monotonic_ns=10'000; x.decode_complete_monotonic_ns=10'500;
    x.decision_monotonic_ns=11'000; x.maximum_leg_skew_ns=100'000'000;
    x.minimum_order_microunits=1'000'000; x.sell_available_microunits=9'000'000;
    x.fee_verified=1; x.fee_rate=0.02; x.fee_exponent=1.0; x.reserve_per_share=0.0005;
    x.yes.valid=x.no.valid=1; x.yes.lineage_continuous=x.no.lineage_continuous=1;
    x.yes.receive_monotonic_ns=9'800; x.no.receive_monotonic_ns=9'700;
    x.yes.tick_size_e4=x.no.tick_size_e4=100;
    x.yes.state_version=21; x.no.state_version=22;
    x.yes.exchange_event_ns=123; x.no.exchange_event_ns=124;
    x.yes.best_bid_e4=3900; x.yes.best_ask_e4=4000;
    x.no.best_bid_e4=4900; x.no.best_ask_e4=5000;
    x.yes.best_bid_microunits=8'000'000; x.no.best_bid_microunits=9'000'000;
    x.yes.best_ask_microunits=6'000'000; x.no.best_ask_microunits=7'000'000;
    x.yes.bid_levels[0]={3900,8'000'000}; x.yes.bid_level_count=1;
    x.no.bid_levels[0]={4900,9'000'000}; x.no.bid_level_count=1;
    x.yes.ask_levels[0]={4000,3'000'000}; x.yes.ask_levels[1]={4100,4'000'000}; x.yes.ask_level_count=2;
    x.no.ask_levels[0]={5000,5'000'000}; x.no.ask_levels[1]={5100,2'000'000}; x.no.ask_level_count=2;
    return x;
}

} // namespace

int main(){
    auto buy=base_input();
    same_plan(evaluate_pair(buy),reference_evaluate(buy));
    same_plan(evaluate_pair(buy),reference_evaluate(buy)); // repeatability

    auto sell=base_input();
    sell.yes.best_bid_e4=6000; sell.yes.best_ask_e4=6100;
    sell.no.best_bid_e4=4500; sell.no.best_ask_e4=4600;
    sell.yes.bid_levels[0]={6000,5'000'000};
    sell.no.bid_levels[0]={4500,5'000'000};
    sell.yes.ask_levels[0]={6100,5'000'000}; sell.yes.ask_level_count=1;
    sell.no.ask_levels[0]={4600,5'000'000}; sell.no.ask_level_count=1;
    sell.sell_available_microunits=4'000'000;
    same_plan(evaluate_pair(sell),reference_evaluate(sell));

    auto skew=buy; skew.no.receive_monotonic_ns=1;
    same_plan(evaluate_pair(skew),reference_evaluate(skew));

    auto lineage=buy; lineage.no.lineage_continuous=0;
    same_plan(evaluate_pair(lineage),reference_evaluate(lineage));

    auto minimum=buy; minimum.minimum_order_microunits=20'000'000;
    same_plan(evaluate_pair(minimum),reference_evaluate(minimum));

    return 0;
}
