#include "pm/v7_probability_model.hpp"
#include <boost/json.hpp>
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <new>

std::atomic<std::uint64_t> allocations{0};
void* operator new(std::size_t n) {allocations.fetch_add(1);if(void* p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void* p) noexcept {std::free(p);}
void operator delete(void* p,std::size_t) noexcept {std::free(p);}
using namespace pm::v7;
namespace json=boost::json;
namespace fs=std::filesystem;
json::object artifact() {
    json::array features,assets,horizons,coefficients,scales,cov;
    for(auto s:kProbabilityFeatureNames)features.emplace_back(std::string(s));
    for(auto s:kProbabilityAssets)assets.emplace_back(std::string(s));
    for(auto s:kProbabilityHorizons)horizons.emplace_back(std::string(s));
    for(int i=0;i<6;++i)scales.emplace_back(1.0);
    for(std::size_t i=0;i<kProbabilityFeatures;++i) {
        coefficients.emplace_back(i==1?1.0:0.0);json::array row;
        for(std::size_t j=0;j<kProbabilityFeatures;++j)row.emplace_back(i==j?.001:0.);
        cov.emplace_back(row);
    }
    return {{"schema","v7_probability_logit_candidate_v1"},{"code_sha",std::string(40,'a')},
      {"paper_only",true},{"real_order_submission",false},{"authenticated_execution",false},
      {"parameters_empirically_fitted",true},{"forward_calibrated",false},
      {"test_duration_seconds",7200},{"excluded_assets",json::array{}},{"asset_shadow_overrides",json::array{}},
      {"feature_schema",features},{"asset_order",assets},{"horizon_order",horizons},
      {"coefficients",coefficients},{"covariance",cov},{"shock_scales",scales},
      {"uncertainty_z",1.645},{"explicit_logit_reserve",.25},
      {"execution_reserve_per_share",.005},{"minimum_net_edge",.02},
      {"fractional_kelly",.25},{"maximum_chase_ticks",2},
      {"maximum_order_cost_microdollars",3750000},
      {"maximum_quantity_microunits",20000000}};
}
NativeCryptoDecisionInput input() {
    NativeCryptoDecisionInput x;x.now_monotonic_ns=10'000'000'000LL;
    x.market.close_monotonic_ns=x.now_monotonic_ns+110'000'000'000LL;
    x.signal.trigger_receive_monotonic_ns=x.now_monotonic_ns-10'000'000;
    x.signal.direction=1;x.signal.binance_return_100ms_bp=.8;x.signal.coinbase_return_100ms_bp=0.;
    x.yes_book.valid=1;x.yes_book.lineage_continuous=1;
    x.yes_book.receive_monotonic_ns=x.now_monotonic_ns-1'000'000;
    x.yes_book.best_bid_e4=3900;x.yes_book.best_ask_e4=4100;
    x.yes_book.best_bid_microunits=6'000'000;x.yes_book.best_ask_microunits=4'000'000;
    x.no_book=x.yes_book;x.no_book.best_bid_e4=5900;x.no_book.best_ask_e4=6100;
    return x;
}
int main(int argc,char** argv) {
    // Optional private-data parity driver. No embedded fitted coefficients.
    if(argc==4 && std::string_view(argv[3])=="--stdin") {
        auto m=NativeProbabilityModel::load(argv[1],argv[2]);std::string line;
        std::cout.precision(17);
        while(std::getline(std::cin,line)) {
            auto r=json::parse(line).as_object();const auto& z=r.at("features").as_object();auto x=input();
            x.signal.direction=json::value_to<std::int8_t>(z.at("direction"));
            x.signal.binance_return_100ms_bp=json::value_to<double>(z.at("binance_return_100ms_bp"));
            const auto* provider=z.if_contains("confirmation_venue");
            if(provider && provider->is_string() && provider->as_string()!="UNKNOWN") {
                const auto name=std::string_view(provider->as_string());
                if(name!="BYBIT" && name!="COINBASE") throw std::invalid_argument("unknown confirmation provider");
                x.signal.confirmation_venue=name=="BYBIT"
                    ? external_fair::VenueId::BybitSpot : external_fair::VenueId::CoinbaseSpot;
                x.signal.confirmation_return_100ms_bp=json::value_to<double>(z.at("confirmation_return_100ms_bp"));
                x.signal.coinbase_return_100ms_bp=std::numeric_limits<double>::quiet_NaN();
            } else {
                x.signal.coinbase_return_100ms_bp=json::value_to<double>(z.at("coinbase_return_100ms_bp"));
            }
            x.signal.trigger_receive_monotonic_ns=x.now_monotonic_ns-json::value_to<std::int64_t>(z.at("signal_age_ns"));
            x.market.close_monotonic_ns=x.now_monotonic_ns+json::value_to<std::int64_t>(z.at("tte_ns"));
            auto& b=x.signal.direction>0?x.yes_book:x.no_book;
            b.best_bid_e4=json::value_to<std::int32_t>(z.at("bid_e4"));
            b.best_ask_e4=json::value_to<std::int32_t>(z.at("ask_e4"));
            b.best_bid_microunits=json::value_to<std::int64_t>(z.at("bid_quantity"));
            b.best_ask_microunits=json::value_to<std::int64_t>(z.at("ask_quantity"));
            auto f=m.predict(x,std::string_view(r.at("asset").as_string()),std::string_view(r.at("horizon").as_string()));
            std::cout<<static_cast<unsigned>(f.valid)<<' '<<f.up<<' '<<f.lower<<' '<<f.upper<<'\n';
        }
        return 0;
    }
    // Optional single synthetic parity fixture.
    if(argc==3) {
        auto m=NativeProbabilityModel::load(argv[1],argv[2]);auto x=input();
        auto f=m.predict(x,"DOGE","M5");
        std::cout.precision(17);std::cout<<f.up<<' '<<f.lower<<' '<<f.upper<<'\n';return 0;
    }
    auto path=fs::temp_directory_path()/"v7-probability-model-test.json";
    auto o=artifact();{std::ofstream f(path);f<<json::serialize(o);}
    auto m=NativeProbabilityModel::load(path.string(),std::string(40,'a'));
    assert(m.loaded && m.artifact_sha256.size()==64);
    auto x=input();const auto before=allocations.load();
    auto f=m.predict(x,"DOGE","M5");assert(allocations.load()==before);
    assert(f.valid && !f.forward_calibrated && std::abs(f.up-.4)<1e-12);
    assert(f.lower<f.up && f.upper>f.up && f.max_input_receive_ns<=f.asof_ns);
    x.signal.direction=-1;f=m.predict(x,"DOGE","M5");
    assert(f.valid && std::abs(f.up-.4)<1e-12);
    x.signal.binance_return_100ms_bp=std::numeric_limits<double>::quiet_NaN();
    assert(!m.predict(x,"DOGE","M5").valid);
    x=input();x.signal.trigger_receive_monotonic_ns=x.now_monotonic_ns+1;
    assert(!m.predict(x,"DOGE","M5").valid);
    assert(!m.predict(input(),"UNKNOWN","M5").valid);
    x=input(); x.signal.confirmation_venue=external_fair::VenueId::BybitSpot;
    x.signal.confirmation_return_100ms_bp=.3;
    x.signal.coinbase_return_100ms_bp=std::numeric_limits<double>::quiet_NaN();
    std::array<double,kProbabilityFeatures> generic_features{};
    assert(probability_features(x,"BNB","M5",m.shock_scales,generic_features));
    assert(std::abs(generic_features[3]-.3)<1e-12);
    assert(m.predict(x,"BNB","M5").valid);
    for(int failure=0;failure<7;++failure) {
        o=artifact();
        if(failure==0)o["code_sha"]=std::string(40,'b');
        if(failure==1)o["real_order_submission"]=true;
        if(failure==2)o["feature_schema"].as_array()[1]="wrong_feature";
        if(failure==3)o["covariance"].as_array()[0].as_array()[0]=-1.;
        if(failure==4)o["maximum_order_cost_microdollars"]=3750001;
        if(failure==5)o["excluded_assets"]=json::array{"DOGE"};
        if(failure==6)o["maximum_chase_ticks"]=std::int64_t{4'294'967'298LL};
        {std::ofstream out(path);out<<json::serialize(o);}
        bool rejected=false;try{(void)NativeProbabilityModel::load(path.string(),std::string(40,'a'));}
        catch(const std::exception&){rejected=true;}assert(rejected);
    }
    fs::remove(path);std::cout<<"model identity, risk, covariance, feature order and causal allocation-free inference passed\n";
}
