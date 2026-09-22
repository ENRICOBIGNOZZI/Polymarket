#include "pm/v7_native_paper_pair_execution.hpp"
#include "pm/v7_native_settlement_authority.hpp"

#include <cassert>
#include <cmath>

using namespace pm::v7;

namespace {

CapitalLimits limits() {
    return {20'000'000,20'000'000,20'000'000,10'000'000};
}

ExecutionPlan plan(
    std::uint64_t intent_id,
    std::uint64_t instrument,
    Side side,
    std::int64_t quantity,
    std::int64_t price_tick) {
    ExecutionPlan out{};
    out.intent.intent_id=intent_id;
    out.intent.market_handle=7;
    out.intent.event_handle=8;
    out.intent.instrument_handle=instrument;
    out.intent.state_version=10+instrument;
    out.intent.causal_trigger_receive_monotonic_ns=900;
    out.intent.decode_complete_monotonic_ns=950;
    out.intent.signal_ready_monotonic_ns=980;
    out.intent.decision_monotonic_ns=1'000;
    out.intent.exchange_event_ns=800;
    out.intent.price_tick=price_tick;
    out.intent.quantity_microunits=quantity;
    out.intent.strategy_id=StrategyId::HardArbitrage;
    out.intent.type=IntentType::TargetPosition;
    out.intent.side=side;
    out.intent.urgency=Urgency::Aggressive;
    out.intent.purpose=IntentPurpose::Alpha;
    out.intent.passive=0;
    out.intent.post_only=0;
    out.tick_size_e4=100;
    out.market_state_version=out.intent.state_version;
    out.policy=ExecutionPolicyId::PureArbFok;
    return out;
}

BookHotSnapshot book(
    std::int32_t bid,
    std::int32_t ask,
    std::int64_t receive_ns) {
    BookHotSnapshot b{};
    b.valid=1;
    b.lineage_continuous=1;
    b.tick_size_e4=100;
    b.receive_monotonic_ns=receive_ns;
    b.exchange_event_ns=receive_ns-100;
    b.state_version=1;
    b.best_bid_e4=bid;
    b.best_ask_e4=ask;
    b.best_bid_microunits=10'000'000;
    b.best_ask_microunits=10'000'000;
    b.bid_levels[0]={bid,6'000'000};
    b.bid_levels[1]={bid-100,4'000'000};
    b.ask_levels[0]={ask,6'000'000};
    b.ask_levels[1]={ask+100,4'000'000};
    b.bid_level_count=2;
    b.ask_level_count=2;
    return b;
}

NativeSettlementPairResult admit(
    NativeSettlementAuthority& authority,
    const ExecutionPlan& yes,
    const ExecutionPlan& no,
    std::int64_t now=1'100) {
    return authority.submit_pair(yes,no,5'000'000,now);
}

} // namespace

int main() {
    {
        NativeSettlementAuthority authority(limits());
        NativeSettlementOmsEndpoint endpoint(authority);
        NativePaperPairExecutionAdapter adapter(endpoint,0);
        auto yes=plan(1,11,Side::Buy,10'000'000,41);
        auto no=plan(2,12,Side::Buy,10'000'000,51);
        const auto admitted=admit(authority,yes,no);
        assert(admitted.accepted);
        auto yes_book=book(3900,4000,1'050);
        auto no_book=book(4900,5000,1'050);
        const auto result=adapter.submit(
            admitted.yes.tx.command,admitted.no.tx.command,
            yes_book,no_book,1'200);
        assert(result.accepted);
        assert(result.reason==NativePaperPairReason::Accepted);
        assert(result.yes.final_state==OrderState::Filled);
        assert(result.no.final_state==OrderState::Filled);
        assert(result.yes.levels_used==2);
        assert(result.no.levels_used==2);
        assert(std::abs(result.yes.vwap_e4-4040.0)<1e-12);
        assert(std::abs(result.no.vwap_e4-5040.0)<1e-12);
        assert(adapter.complete_pairs()==1);
        assert(authority.active_orders()==0);
        assert(authority.inventory_snapshot(11).total_microunits==10'000'000);
        assert(authority.inventory_snapshot(12).total_microunits==10'000'000);
        assert(authority.capital_snapshot().order_reserved_microdollars==0);
    }
    {
        NativeSettlementAuthority authority(limits());
        NativeSettlementOmsEndpoint endpoint(authority);
        NativePaperPairExecutionAdapter adapter(endpoint,0);
        auto yes=plan(3,11,Side::Buy,10'000'000,41);
        auto no=plan(4,12,Side::Buy,10'000'000,50);
        const auto admitted=admit(authority,yes,no);
        assert(admitted.accepted);
        auto yes_book=book(3900,4000,1'050);
        auto no_book=book(4900,5000,1'050);
        no_book.ask_levels[0].quantity_microunits=4'000'000;
        no_book.ask_level_count=1;
        no_book.best_ask_microunits=4'000'000;
        const auto result=adapter.submit(
            admitted.yes.tx.command,admitted.no.tx.command,
            yes_book,no_book,1'200);
        assert(!result.accepted);
        assert(result.reason==NativePaperPairReason::InsufficientDepth);
        assert(result.yes.final_state==OrderState::Rejected);
        assert(result.no.final_state==OrderState::Rejected);
        assert(adapter.rejected_pairs()==1);
        assert(authority.active_orders()==0);
        assert(authority.capital_snapshot().order_reserved_microdollars==0);
        assert(authority.inventory_snapshot(11).total_microunits==0);
        assert(authority.inventory_snapshot(12).total_microunits==0);
    }
    {
        NativeSettlementAuthority authority(limits());
        NativeSettlementOmsEndpoint endpoint(authority);
        NativePaperPairExecutionAdapter adapter(endpoint,100,500);
        auto yes=plan(5,11,Side::Buy,10'000'000,41);
        auto no=plan(6,12,Side::Buy,10'000'000,51);
        const auto admitted=admit(authority,yes,no);
        assert(admitted.accepted);
        auto yes_book=book(3900,4000,1'050);
        auto no_book=book(4900,5000,1'050);
        const auto pending=adapter.submit(
            admitted.yes.tx.command,admitted.no.tx.command,
            yes_book,no_book,1'200);
        assert(pending.accepted && pending.pending_arrival);
        assert(pending.scheduled_arrival_ns==1'300);
        assert(adapter.pending_arrivals()==1);
        // First post-deadline watermark. Books are the latest causal state
        // consumed before 1300 and are within the allowed age.
        yes_book.receive_monotonic_ns=1'250;
        no_book.receive_monotonic_ns=1'240;
        const auto batch=adapter.advance_arrivals(
            yes_book,no_book,1'301);
        assert(!batch.invalid);
        assert(batch.count==1);
        assert(batch.records[0].accepted);
        assert(batch.records[0].reason==NativePaperPairReason::Accepted);
        assert(adapter.pending_arrivals()==0);
        assert(authority.active_orders()==0);
    }
    return 0;
}
