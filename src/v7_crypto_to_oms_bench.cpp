#include "pm/v7_crypto_decision_lane.hpp"
#include "pm/v7_native_order_tx.hpp"

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string_view>
#include <vector>

using namespace pm::v7;
using namespace pm::v7::external_fair;

namespace {
std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
std::size_t samples(int argc, char** argv) {
    std::size_t value = 200000;
    for (int i=1;i<argc;++i) {
        if (std::string_view(argv[i]) == "--samples" && i+1<argc) {
            auto text=std::string_view(argv[++i]);
            auto result=std::from_chars(text.data(),text.data()+text.size(),value);
            if (result.ec!=std::errc{} || result.ptr!=text.data()+text.size()
                || value<100 || value>2'000'000) return 0;
        } else return 0;
    }
    return value;
}
std::int64_t quantile(const std::vector<std::int64_t>& v, double p) {
    auto index=static_cast<std::size_t>(p*static_cast<double>(v.size()-1));
    return v[index];
}
}

int main(int argc,char**argv) {
    const auto n=samples(argc,argv); if(!n) return 64;
    NativeCryptoDecisionPolicy policy;
    NativeCryptoDecisionLane lane(policy);
    NativeOrderTxOwner owner;
    CapitalLimits limits{10'000'000'000LL,10'000'000'000LL,10'000'000'000LL,10'000'000'000LL};
    SleeveCapitalAccount capital(limits);
    NativeCryptoDecisionInput input;
    input.market.market_handle=7; input.market.event_handle=8;
    input.market.yes={11,1'000'000,1,{}}; input.market.no={12,1'000'000,0,{}};
    input.market.accepting_orders=1; input.market.contract_verified=1;
    input.market.settlement_reference_valid=1;
    input.yes_book.tick_size_e4=100; input.yes_book.best_bid_e4=3900; input.yes_book.best_ask_e4=4000;
    input.yes_book.best_bid_microunits=10'000'000; input.yes_book.best_ask_microunits=10'000'000;
    input.yes_book.state_version=1; input.yes_book.lineage_continuous=1; input.yes_book.valid=1;
    input.no_book=input.yes_book; input.no_book.best_bid_e4=5900; input.no_book.best_ask_e4=6000;
    input.signal.confirmed_non_opposing=1; input.signal.direction=1; input.signal.valid=1;
    input.signal.binance_return_100ms_bp=.5; input.signal.coinbase_return_100ms_bp=.2;
    std::vector<std::int64_t> latency; latency.reserve(n);
    std::uint64_t event_id=10'000'000;
    for(std::size_t i=0;i<n;++i) {
        const auto t=now_ns();
        lane.reset_market(7);
        input.now_monotonic_ns=t;
        input.market.close_monotonic_ns=t+110'000'000'000LL;
        input.yes_book.receive_monotonic_ns=t-1'000;
        input.no_book.receive_monotonic_ns=t-1'000;
        input.signal.signal_version=static_cast<std::uint64_t>(i+1);
        input.signal.trigger_receive_monotonic_ns=t-10'000;
        input.signal.valid_until_monotonic_ns=t+100'000'000;
        const auto started=now_ns();
        const auto decision=lane.evaluate(input,capital);
        if(!decision.accepted) return 2;
        ExecutionPlan plan;
        plan.intent=decision.intent;
        plan.tick_size_e4=input.yes_book.tick_size_e4;
        plan.market_state_version=input.yes_book.state_version;
        plan.policy=ExecutionPolicyId::AggressiveTaker;
        const auto prepared=owner.prepare_submit(plan,now_ns());
        const auto finished=now_ns();
        if(!prepared.accepted || prepared.oms.state!=OrderState::SendPending) return 3;
        latency.push_back(finished-started);
        if(!capital.release_order(decision.intent.intent_id)) return 4;
        OmsEvent reject;
        reject.event_id=++event_id; reject.type=OmsEventType::Reject; reject.timestamp_ns=finished+1;
        const auto state=owner.apply(prepared.command.client_order_id,reject);
        if(state.state!=OrderState::Rejected || !owner.retire_terminal(prepared.command.client_order_id)) return 5;
    }
    std::sort(latency.begin(),latency.end());
    std::cout << "{\"schema\":\"polymarket_v7_crypto_to_oms_bench_v1\","
              << "\"paper_only\":true,\"authenticated_execution\":false,\"real_order_submission\":false,"
              << "\"scope\":\"CPP_SIGNAL_TO_OMS_SEND_PENDING_AND_ADAPTER_COMMAND\","
              << "\"samples\":" << n << ",\"latency_ns\":{"
              << "\"p50\":" << quantile(latency,.50) << ","
              << "\"p95\":" << quantile(latency,.95) << ","
              << "\"p99\":" << quantile(latency,.99) << ","
              << "\"p999\":" << quantile(latency,.999) << ","
              << "\"max\":" << latency.back() << "}}\n";
    return 0;
}
