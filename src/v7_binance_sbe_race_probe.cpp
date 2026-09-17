#include "pm/v7_binance_sbe.hpp"
#include "pm/v7_external_ws.hpp"

#include <boost/json.hpp>
#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <string>
#include <thread>
#include <vector>
#if !defined(__APPLE__)
#include <stop_token>
#endif

namespace json = boost::json;
using namespace pm::v7::external_fair;
using namespace std::chrono_literals;
namespace {
struct Quote { std::int64_t receive_ns=0; double bid=0,bid_qty=0,ask=0,ask_qty=0; };
std::int64_t now_ns() { return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count(); }
int bounded(std::string_view text, int lo, int hi) {
    int value=0; auto r=std::from_chars(text.data(), text.data()+text.size(), value);
    if (r.ec != std::errc{} || r.ptr != text.data()+text.size() || value<lo || value>hi)
        throw std::invalid_argument("bounded integer argument required");
    return value;
}
bool number(const json::value* value, double& out) {
    if (!value) return false;
    try {
        if (value->is_string()) out=std::stod(std::string(value->as_string()));
        else if (value->is_double()) out=value->as_double();
        else if (value->is_int64()) out=static_cast<double>(value->as_int64());
        else if (value->is_uint64()) out=static_cast<double>(value->as_uint64());
        else return false;
        return std::isfinite(out);
    } catch (...) { return false; }
}
std::uint64_t sequence(const json::value* value) {
    if (!value) return 0;
    if (value->is_uint64()) return value->as_uint64();
    if (value->is_int64() && value->as_int64()>0) return static_cast<std::uint64_t>(value->as_int64());
    return 0;
}
class JsonObserver final : public ExternalFrameObserver {
public:
    explicit JsonObserver(std::atomic<std::int64_t>& begin):begin_(begin){}
    std::map<std::uint64_t,Quote> rows; std::atomic<bool> seen{false}; std::uint64_t invalid=0, duplicates=0;
    void on_frame(std::uint64_t, std::int64_t receive, std::int64_t, std::string_view payload) noexcept override {
        try {
            boost::system::error_code ec; auto raw=json::parse(payload,ec);
            if(ec||!raw.is_object()){++invalid;return;} const auto& o=raw.as_object();
            auto itS=o.find("s"); if(itS==o.end()||!itS->value().is_string()||itS->value().as_string()!="BTCUSDT") return;
            const auto id=sequence(o.if_contains("u")); if(!id) return;
            Quote q; q.receive_ns=receive;
            if(!number(o.if_contains("b"),q.bid)||!number(o.if_contains("B"),q.bid_qty)
               ||!number(o.if_contains("a"),q.ask)||!number(o.if_contains("A"),q.ask_qty)
               ||q.bid<=0||q.ask<=0||q.ask<q.bid||q.bid_qty<0||q.ask_qty<0){++invalid;return;}
            seen.store(true,std::memory_order_release); if(receive<begin_.load(std::memory_order_acquire)) return;
            if(!rows.emplace(id,q).second) ++duplicates;
        } catch(...){++invalid;}
    }
private: std::atomic<std::int64_t>& begin_;
};
class SbeObserver final : public ExternalFrameObserver {
public:
    explicit SbeObserver(std::atomic<std::int64_t>& begin):begin_(begin){}
    std::map<std::uint64_t,Quote> rows; std::atomic<bool> seen{false}; std::uint64_t invalid=0, ignored=0, duplicates=0;
    void on_frame(std::uint64_t, std::int64_t, std::int64_t, std::string_view) noexcept override { ++invalid; }
    void on_binary_frame(std::uint64_t, std::int64_t receive, std::int64_t, std::string_view payload) noexcept override {
        BinanceSbeBestBidAsk value; const auto state=parse_binance_sbe_best_bid_ask(payload,value);
        if(state==BinanceSbeParseState::Ignored){++ignored;return;} if(state!=BinanceSbeParseState::Parsed){++invalid;return;}
        if(value.symbol!="BTCUSDT") return; seen.store(true,std::memory_order_release);
        if(receive<begin_.load(std::memory_order_acquire)) return;
        Quote q{receive,value.bid,value.bid_qty,value.ask,value.ask_qty};
        if(!rows.emplace(static_cast<std::uint64_t>(value.book_update_id),q).second) ++duplicates;
    }
private: std::atomic<std::int64_t>& begin_;
};
json::object dist(std::vector<double> values){
    if(values.empty()) return {{"count",0},{"p50_us",nullptr},{"p95_us",nullptr},{"p99_us",nullptr},{"min_us",nullptr},{"max_us",nullptr}};
    std::sort(values.begin(),values.end()); auto q=[&](double p){return values[static_cast<std::size_t>(p*(values.size()-1))];};
    return {{"count",values.size()},{"p50_us",q(.50)},{"p95_us",q(.95)},{"p99_us",q(.99)},{"min_us",values.front()},{"max_us",values.back()}};
}
bool same(const Quote&a,const Quote&b){return std::abs(a.bid-b.bid)<1e-10&&std::abs(a.ask-b.ask)<1e-10&&std::abs(a.bid_qty-b.bid_qty)<1e-10&&std::abs(a.ask_qty-b.ask_qty)<1e-10;}
}
int main(int argc,char**argv){
    try{
        int duration=60,warmup=3; bool validate=false;
        for(int i=1;i<argc;++i){std::string_view a=argv[i]; if(a=="--duration-seconds"&&i+1<argc)duration=bounded(argv[++i],1,300); else if(a=="--warmup-seconds"&&i+1<argc)warmup=bounded(argv[++i],1,30); else if(a=="--validate-only")validate=true; else throw std::invalid_argument("unknown/incomplete argument");}
        ExternalVenueConnectionSpec jsonspec=btc_spot_connection_spec(VenueId::BinanceSpot,500);
        jsonspec.target="/ws/btcusdt@bookTicker"; jsonspec.subscription_json.clear(); jsonspec.start_without_subscription=true;
        ExternalVenueConnectionSpec sbespec=btc_spot_connection_spec(VenueId::BinanceSpot,500);
        sbespec.host="stream-sbe.binance.com"; sbespec.port="9443"; sbespec.target="/ws/btcusdt@bestBidAsk";
        sbespec.subscription_json.clear(); sbespec.start_without_subscription=true;
        if(validate){std::cout<<"Binance JSON/SBE race configuration PASS\n";return 0;}
        const char* key=std::getenv("BINANCE_SBE_API_KEY"); if(!key||!*key) throw std::runtime_error("BINANCE_SBE_API_KEY is required");
        sbespec.handshake_headers.emplace_back("X-MBX-APIKEY",key);
        std::atomic<std::int64_t> begin{std::numeric_limits<std::int64_t>::max()}; JsonObserver jo(begin); SbeObserver so(begin);
        ExternalVenueWsClient jc(jsonspec,nullptr,&jo), sc(sbespec,nullptr,&so);
#if defined(__APPLE__)
        std::atomic<bool> stopping{false}; ExternalStopToken token(stopping);
#else
        std::stop_source stopping; auto token=stopping.get_token();
#endif
        std::thread jt([&]{jc.run(token);}), st([&]{sc.run(token);});
        const auto ready=now_ns()+15'000'000'000LL; while(now_ns()<ready && !(jo.seen.load()&&so.seen.load())) std::this_thread::sleep_for(20ms);
        const bool both=jo.seen.load()&&so.seen.load(); const auto start=now_ns()+static_cast<std::int64_t>(warmup)*1'000'000'000LL; begin.store(start,std::memory_order_release);
        const auto finish=start+static_cast<std::int64_t>(duration)*1'000'000'000LL; while(now_ns()<finish)std::this_thread::sleep_for(20ms);
#if defined(__APPLE__)
        stopping.store(true,std::memory_order_release);
#else
        stopping.request_stop();
#endif
        jt.join();st.join();
        std::vector<double> sbe_minus_json, saving; std::uint64_t matched=0,conflicts=0,sbe_first=0,json_first=0,ties=0;
        for(const auto&[id,j]:jo.rows){auto it=so.rows.find(id);if(it==so.rows.end())continue;if(!same(j,it->second)){++conflicts;continue;}++matched;double d=(it->second.receive_ns-j.receive_ns)/1000.0;sbe_minus_json.push_back(d);saving.push_back(-d);if(d<0)++sbe_first;else if(d>0)++json_first;else ++ties;}
        const auto js=jc.snapshot(), ss=sc.snapshot(); const bool clean=both&&jo.invalid==0&&so.invalid==0&&js.transport_failures==0&&ss.transport_failures==0;
        std::cout<<json::serialize(json::object{{"schema","polymarket_v7_binance_json_sbe_race_v1"},{"paper_only",true},{"authenticated_execution",false},{"real_order_submission",false},{"scope","SAME_HOST_SAME_UPDATE_ID_MARKET_DATA_ARRIVAL_ONLY"},{"clean_capture",clean},{"duration_seconds",duration},{"json_records",jo.rows.size()},{"sbe_records",so.rows.size()},{"matched_identical",matched},{"conflicts",conflicts},{"json_invalid",jo.invalid},{"sbe_invalid",so.invalid},{"sbe_ignored",so.ignored},{"sbe_first",sbe_first},{"json_first",json_first},{"ties",ties},{"sbe_minus_json_us",dist(std::move(sbe_minus_json))},{"sbe_saving_vs_json_us",dist(std::move(saving))}})<<'\n';
        return clean?0:2;
    }catch(const std::exception&e){std::cerr<<"binance_sbe_race_probe: "<<e.what()<<'\n';return 64;}
}
