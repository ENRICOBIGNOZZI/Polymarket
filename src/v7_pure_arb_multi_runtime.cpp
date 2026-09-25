#include "pm/fast_ws.hpp"
#include "pm/v7_native_latency_tape.hpp"
#include "pm/v7_native_paper_execution.hpp"
#include "pm/v7_native_settlement_oms_endpoint.hpp"
#include "pm/v7_pure_arb_multi_ledger.hpp"
#include "pm/v7_pure_arb_multi_engine.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace json = boost::json;
using namespace pm::v7;
using namespace pm::v7::pure_arb;
using namespace std::chrono_literals;

namespace {
std::atomic<bool> shutdown_requested{false};
void request_shutdown(int) noexcept {
    shutdown_requested.store(true, std::memory_order_relaxed);
}
std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

const json::object& object_at(const json::object& o, std::string_view key) {
    const auto* value = o.if_contains(key);
    if (!value || !value->is_object()) throw std::invalid_argument("object field required");
    return value->as_object();
}
const json::array& array_at(const json::object& o, std::string_view key) {
    const auto* value = o.if_contains(key);
    if (!value || !value->is_array()) throw std::invalid_argument("array field required");
    return value->as_array();
}
std::string string_at(const json::object& o, std::string_view key) {
    const auto* value=o.if_contains(key);
    if(!value || !value->is_string()) throw std::invalid_argument("string field required");
    return std::string(value->as_string());
}
std::int64_t int_at(const json::object& o, std::string_view key) {
    const auto* value=o.if_contains(key);
    if(!value) throw std::invalid_argument("integer field required");
    if(value->is_int64()) return value->as_int64();
    if(value->is_uint64() && value->as_uint64()
       <= static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        return static_cast<std::int64_t>(value->as_uint64());
    throw std::invalid_argument("integer field required");
}
double double_at(const json::object& o, std::string_view key) {
    const auto* value=o.if_contains(key);
    if(!value) throw std::invalid_argument("number field required");
    if(value->is_double()) return value->as_double();
    if(value->is_int64()) return static_cast<double>(value->as_int64());
    if(value->is_uint64()) return static_cast<double>(value->as_uint64());
    throw std::invalid_argument("number field required");
}
bool bool_at(const json::object& o, std::string_view key) {
    const auto* value=o.if_contains(key);
    if(!value || !value->is_bool()) throw std::invalid_argument("boolean field required");
    return value->as_bool();
}
json::object load(std::string_view path) {
    std::ifstream input{std::string(path)};
    if(!input) throw std::invalid_argument("config unavailable");
    std::string bytes((std::istreambuf_iterator<char>(input)),
                      std::istreambuf_iterator<char>());
    boost::system::error_code ec;
    auto value=json::parse(bytes,ec);
    if(ec || !value.is_object()) throw std::invalid_argument("config JSON invalid");
    return value.as_object();
}

struct MarketRuntimeConfig {
    MultiMarketConfig arb{};
    std::string market_id;
    std::string event_id;
    std::string asset;
    std::string horizon;
    std::string fee_source;
    std::string yes_token;
    std::string no_token;
    std::int32_t yes_tick_e4=0;
    std::int32_t no_tick_e4=0;
    std::int64_t yes_inventory=0;
    std::int64_t no_inventory=0;
    std::int64_t yes_basis=0;
    std::int64_t no_basis=0;
};
struct Config {
    std::string pm_ws_url;
    std::string latency_tape;
    std::string run_root;
    std::string model_sha;
    std::string run_id;
    std::string server_id;
    std::string risk_policy_sha256;
    CapitalLimits capital{};
    std::vector<MarketRuntimeConfig> markets;
};
Config parse_config(std::string_view path) {
    const auto root=load(path);
    if(string_at(root,"schema")!="polymarket_v7_pure_arb_multi_runtime_v1"
       || !bool_at(root,"paper_only")
       || bool_at(root,"authenticated_execution")
       || bool_at(root,"real_order_submission")) {
        throw std::invalid_argument("PAPER runtime identity invalid");
    }
    Config out{};
    out.pm_ws_url=string_at(root,"pm_ws_url");
    out.latency_tape=string_at(root,"latency_tape");
    out.run_root=string_at(root,"run_root");
    out.model_sha=string_at(root,"model_sha");
    out.run_id=string_at(root,"run_id");
    out.server_id=string_at(root,"server_id");
    out.risk_policy_sha256=string_at(root,"risk_policy_sha256");
    if(out.run_root.empty() || out.model_sha.size()!=40 || out.run_id.empty()
       || out.server_id.empty() || out.risk_policy_sha256.size()!=64)
        throw std::invalid_argument("ledger identity invalid");
    const auto& capital=object_at(root,"capital_limits");
    out.capital.sleeve_budget_microdollars=int_at(capital,"sleeve_budget_microdollars");
    out.capital.max_total_exposure_microdollars=int_at(capital,"max_total_exposure_microdollars");
    out.capital.max_market_exposure_microdollars=int_at(capital,"max_market_exposure_microdollars");
    out.capital.max_single_order_microdollars=int_at(capital,"max_single_order_microdollars");
    if(!out.capital.valid()) throw std::invalid_argument("capital limits invalid");

    const auto& rows=array_at(root,"markets");
    if(rows.empty() || rows.size()>kPureArbMultiMarketCapacity)
        throw std::invalid_argument("market count invalid");
    out.markets.reserve(rows.size());
    for(const auto& raw:rows){
        if(!raw.is_object()) throw std::invalid_argument("market row invalid");
        const auto& row=raw.as_object();
        MarketRuntimeConfig m{};
        m.arb.market_handle=static_cast<std::uint64_t>(int_at(row,"market_handle"));
        m.arb.event_handle=static_cast<std::uint64_t>(int_at(row,"event_handle"));
        m.market_id=string_at(row,"market_id");
        m.event_id=string_at(row,"event_id");
        m.asset=string_at(row,"asset");
        m.horizon=string_at(row,"horizon");
        m.fee_source=string_at(row,"fee_source");
        m.arb.yes_instrument_handle=static_cast<std::uint64_t>(int_at(row,"yes_instrument_handle"));
        m.arb.no_instrument_handle=static_cast<std::uint64_t>(int_at(row,"no_instrument_handle"));
        m.arb.market_start_wall_ms=int_at(row,"market_start_wall_ms");
        m.arb.market_end_wall_ms=int_at(row,"market_end_wall_ms");
        m.arb.maximum_leg_skew_ns=int_at(row,"maximum_leg_skew_ns");
        m.arb.minimum_order_microunits=int_at(row,"minimum_order_microunits");
        m.arb.fee_rate=double_at(row,"fee_rate");
        m.arb.fee_exponent=double_at(row,"fee_exponent");
        m.arb.reserve_per_share=double_at(row,"reserve_per_share");
        m.arb.fee_verified=bool_at(row,"fee_verified")?1:0;
        m.yes_token=string_at(row,"yes_token");
        m.no_token=string_at(row,"no_token");
        m.yes_tick_e4=static_cast<std::int32_t>(int_at(row,"yes_tick_size_e4"));
        m.no_tick_e4=static_cast<std::int32_t>(int_at(row,"no_tick_size_e4"));
        m.yes_inventory=int_at(row,"yes_inventory_microunits");
        m.no_inventory=int_at(row,"no_inventory_microunits");
        m.yes_basis=int_at(row,"yes_collateral_basis_microdollars");
        m.no_basis=int_at(row,"no_collateral_basis_microdollars");
        if(m.market_id.empty() || m.event_id.empty() || m.asset.empty()
           || m.horizon.empty() || m.fee_source.empty()
           || m.yes_token.empty() || m.no_token.empty()
           || m.yes_tick_e4<=0 || m.no_tick_e4<=0
           || m.yes_inventory<0 || m.no_inventory<0
           || m.yes_basis<0 || m.no_basis<0) {
            throw std::invalid_argument("market execution terms invalid");
        }
        out.markets.push_back(std::move(m));
    }
    return out;
}

struct Options {
    std::string config;
    int duration_seconds=0;
    bool validate_only=false;
};
Options options(int argc,char**argv){
    Options out{};
    for(int i=1;i<argc;++i){
        const std::string_view arg=argv[i];
        if(arg=="--config" && i+1<argc) out.config=argv[++i];
        else if(arg=="--duration-seconds" && i+1<argc){
            out.duration_seconds=std::stoi(argv[++i]);
            if(out.duration_seconds<0 || out.duration_seconds>86'400)
                throw std::invalid_argument("duration invalid");
        } else if(arg=="--validate-only") out.validate_only=true;
        else throw std::invalid_argument("unknown option");
    }
    if(out.config.empty()) throw std::invalid_argument("--config required");
    return out;
}
}

int main(int argc,char**argv){
    try{
        const auto opt=options(argc,argv);
        auto config=parse_config(opt.config);

        std::vector<MultiMarketConfig> arb_configs;
        std::vector<TokenBinding> bindings;
        std::vector<std::string> token_ids;
        arb_configs.reserve(config.markets.size());
        bindings.reserve(config.markets.size()*2);
        token_ids.reserve(config.markets.size()*2);
        for(const auto& m:config.markets){
            arb_configs.push_back(m.arb);
            bindings.push_back({m.yes_token,m.arb.market_handle,m.arb.event_handle,
                                m.arb.yes_instrument_handle,m.yes_tick_e4});
            bindings.push_back({m.no_token,m.arb.market_handle,m.arb.event_handle,
                                m.arb.no_instrument_handle,m.no_tick_e4});
            token_ids.push_back(m.yes_token);
            token_ids.push_back(m.no_token);
        }

        NativeSettlementAuthority authority(config.capital);
        PureArbMultiLedgerConfig ledger_config{};
        ledger_config.run_root=config.run_root;
        ledger_config.model_sha=config.model_sha;
        ledger_config.run_id=config.run_id;
        ledger_config.server_id=config.server_id;
        ledger_config.risk_policy_sha256=config.risk_policy_sha256;
        ledger_config.markets.reserve(config.markets.size());
        for(const auto& m:config.markets){
            ledger_config.markets.push_back(PureArbLedgerMarket{
                m.market_id,m.event_id,m.asset,m.horizon,
                m.yes_token,m.no_token,m.fee_source,
                m.arb.yes_instrument_handle,m.arb.no_instrument_handle,
                m.arb.fee_rate,m.arb.fee_exponent});
        }
        PureArbMultiLedgerWriter ledger(std::move(ledger_config));
        if(ledger.snapshot().healthy==0)
            throw std::runtime_error("multi ledger writer unavailable");
        for(const auto& m:config.markets){
            if(!authority.sync_inventory(m.arb.market_handle,
                    m.arb.yes_instrument_handle,m.yes_inventory,m.yes_basis,1)
               || !authority.sync_inventory(m.arb.market_handle,
                    m.arb.no_instrument_handle,m.no_inventory,m.no_basis,1)) {
                throw std::runtime_error("inventory bootstrap failed");
            }
        }
        NativeSettlementOmsEndpoint endpoint(authority);
        NativePaperExecutionAdapter paper(endpoint);
        NativeLatencyTape tape(config.latency_tape);
        if(tape.snapshot().healthy==0)
            throw std::runtime_error("latency tape unavailable");
        MultiMarketEngine engine(arb_configs,authority,&tape);
        if(!engine.valid()) throw std::runtime_error("multi-market engine invalid");

        if(opt.validate_only){
            tape.stop();
            std::cout<<json::serialize(json::object{
                {"schema","polymarket_v7_pure_arb_multi_runtime_validation_v1"},
                {"valid",true},{"paper_only",true},
                {"authenticated_execution",false},
                {"real_order_submission",false},
                {"market_count",engine.market_count()},
                {"pm_worker_count",1}})<<'\n';
            return 0;
        }

        MarketWsShard decoder(std::move(bindings));
        auto scratch=std::make_unique<std::array<MarketWsEvent,1024>>();
        std::uint64_t epoch=1;
        std::uint64_t frames=0,decoded_events=0,evaluations=0,admissions=0;
        std::uint64_t paired_fills=0,no_fills=0,invalid_pairs=0,lineage_faults=0;
        std::uint64_t one_leg_fills=0;

        pm::fast::MarketWebSocketFeed feed(
            config.pm_ws_url,std::move(token_ids),
            config.markets.size()*2,
            [&](std::string_view payload,const pm::fast::FeedReceiveStamp& stamp,std::size_t){
                ++frames;
                auto& output=*scratch;
                const auto result=decoder.process_frame(payload,stamp,output);
                if(result.invalid_frame || result.output_overflow
                   || result.arena_exhausted || result.lineage_invalidated){
                    ++lineage_faults;
                    engine.invalidate_all();
                    ++epoch; if(epoch==0) ++epoch;
                    return;
                }
                for(std::size_t i=0;i<result.output_count;++i){
                    ++decoded_events;
                    auto decision=engine.on_market_event(output[i],epoch,stamp.wall_ms);
                    if(decision.evaluated==0) continue;
                    ++evaluations;
                    if(decision.admitted==0) continue;
                    ++admissions;
                    BookHotSnapshot yes{},no{};
                    if(!engine.current_books(decision.context_index,yes,no)){
                        ++invalid_pairs;
                        continue;
                    }
                    const auto executed=paper.submit_pair_fok(
                        decision.admission.yes.tx.command,yes,
                        decision.admission.no.tx.command,no,
                        monotonic_ns());
                    paired_fills+=executed.paired_fill;
                    one_leg_fills+=executed.one_leg_fill;
                    if(executed.invalid) ++invalid_pairs;
                    if(!executed.accepted) {
                        ++no_fills;
                    } else if(executed.paired_fill!=0) {
                        if(!ledger.publish(decision.context_index,executed.yes.fill)
                           || !ledger.publish(decision.context_index,executed.no.fill)) {
                            ++invalid_pairs;
                        }
                    }
                }
            },
            [&](std::size_t,std::string_view){
                decoder.invalidate_all_lineage();
                engine.invalidate_all();
                ++lineage_faults;
                ++epoch; if(epoch==0) ++epoch;
            });

        std::signal(SIGINT,request_shutdown);
        std::signal(SIGTERM,request_shutdown);
        feed.start();
        const auto started=std::chrono::steady_clock::now();
        while(!shutdown_requested.load(std::memory_order_relaxed)){
            if(opt.duration_seconds>0
               && std::chrono::steady_clock::now()-started
                  >= std::chrono::seconds(opt.duration_seconds)) break;
            std::this_thread::sleep_for(50ms);
        }
        feed.stop();
        tape.stop();
        ledger.stop();
        const auto feed_status=feed.snapshot();
        const auto tape_status=tape.snapshot();
        const auto ledger_status=ledger.snapshot();
        const bool clean=feed_status.workers==1
            && one_leg_fills==0 && invalid_pairs==0
            && tape_status.dropped==0 && tape_status.queued==0
            && tape_status.published==tape_status.written
            && ledger_status.healthy!=0 && ledger_status.dropped==0
            && ledger_status.queued==0
            && ledger_status.published==ledger_status.written;
        std::cout<<json::serialize(json::object{
            {"schema","polymarket_v7_pure_arb_multi_runtime_v1"},
            {"paper_only",true},{"authenticated_execution",false},
            {"real_order_submission",false},{"real_capital_at_risk",false},
            {"single_process",true},{"pm_worker_count",feed_status.workers},
            {"market_count",engine.market_count()},
            {"frames",frames},{"decoded_events",decoded_events},
            {"evaluations",evaluations},{"pair_admissions",admissions},
            {"paired_fills",paired_fills},{"one_leg_fills",one_leg_fills},
            {"no_fills",no_fills},{"invalid_pairs",invalid_pairs},
            {"lineage_faults",lineage_faults},
            {"pm_messages",feed_status.messages},
            {"pm_reconnects",feed_status.reconnects},
            {"pm_errors",feed_status.errors},
            {"direct_decision_queue_depth",0},
            {"latency_queue_depth",tape_status.queued},
            {"latency_published",tape_status.published},
            {"latency_written",tape_status.written},
            {"latency_dropped",tape_status.dropped},
            {"ledger_published",ledger_status.published},
            {"ledger_written",ledger_status.written},
            {"ledger_dropped",ledger_status.dropped},
            {"ledger_queue_depth",ledger_status.queued},
            {"clean",clean}})<<'\n';
        return clean?0:2;
    }catch(const std::exception& e){
        std::cerr<<e.what()<<'\n';
        return 64;
    }
}
