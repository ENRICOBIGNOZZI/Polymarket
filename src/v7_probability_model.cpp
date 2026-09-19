#include "pm/v7_probability_model.hpp"
#include <boost/json.hpp>
#include <openssl/evp.h>
#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace pm::v7 {
namespace json=boost::json;
namespace {
double number(const json::value& v) {
    const double x=json::value_to<double>(v);
    if (!std::isfinite(x)) throw std::invalid_argument("probability nonfinite parameter");
    return x;
}
double sigmoid(double x) noexcept {return 1./(1.+std::exp(-std::clamp(x,-40.,40.)));}
}
NativeProbabilityModel NativeProbabilityModel::load(
    const std::string& path,std::string_view expected_code_sha) {
    if(expected_code_sha.size()!=40 || !std::all_of(expected_code_sha.begin(),expected_code_sha.end(),
        [](char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');}))
        throw std::invalid_argument("probability exact code SHA required");
    std::ifstream f(path,std::ios::binary|std::ios::ate);
    if(!f || f.tellg()<=0 || f.tellg()>1024*1024) throw std::invalid_argument("probability artifact file");
    std::string bytes(static_cast<std::size_t>(f.tellg()),'\0');f.seekg(0);f.read(bytes.data(),bytes.size());
    if(!f) throw std::invalid_argument("probability artifact read");
    const auto o=json::parse(bytes).as_object();
    if(o.at("schema").as_string()!="v7_probability_logit_candidate_v1"
        || std::string_view(o.at("code_sha").as_string())!=expected_code_sha
        || !o.at("paper_only").as_bool() || o.at("real_order_submission").as_bool()
        || o.at("authenticated_execution").as_bool()
        || !o.at("parameters_empirically_fitted").as_bool()
        || o.at("forward_calibrated").as_bool()
        || o.at("test_duration_seconds").as_int64()!=7200
        || !o.at("excluded_assets").as_array().empty()
        || !o.at("asset_shadow_overrides").as_array().empty())
        throw std::invalid_argument("probability identity/safety/experiment contract");
    const auto validate_names=[&](const char* key,const auto& names) {
        const auto& xs=o.at(key).as_array();
        if(xs.size()!=names.size())throw std::invalid_argument("probability feature dimensions");
        for(std::size_t i=0;i<names.size();++i)
            if(std::string_view(xs[i].as_string())!=names[i])throw std::invalid_argument("probability feature order");
    };
    validate_names("feature_schema",kProbabilityFeatureNames);
    validate_names("asset_order",kProbabilityAssets);
    validate_names("horizon_order",kProbabilityHorizons);
    NativeProbabilityModel model;
    const auto& b=o.at("coefficients").as_array();const auto& cov=o.at("covariance").as_array();
    if(b.size()!=kProbabilityFeatures || cov.size()!=kProbabilityFeatures)
        throw std::invalid_argument("probability coefficient dimensions");
    for(std::size_t i=0;i<kProbabilityFeatures;++i) {
        model.coefficients[i]=number(b[i]);
        if(std::abs(model.coefficients[i])>100)throw std::invalid_argument("probability coefficient range");
        const auto& row=cov[i].as_array();
        if(row.size()!=kProbabilityFeatures)throw std::invalid_argument("probability covariance dimensions");
        for(std::size_t j=0;j<kProbabilityFeatures;++j)model.covariance[i][j]=number(row[j]);
    }
    // Cholesky with a tiny numerical diagonal guards against negative variance
    // or a corrupt uncertainty matrix; it does not certify interval coverage.
    std::array<std::array<double,kProbabilityFeatures>,kProbabilityFeatures> L{};
    for(std::size_t i=0;i<kProbabilityFeatures;++i) {
        for(std::size_t j=0;j<=i;++j) {
            if(std::abs(model.covariance[i][j]-model.covariance[j][i])>1e-8)
                throw std::invalid_argument("probability covariance asymmetry");
            double v=model.covariance[i][j]+(i==j?1e-10:0.);
            for(std::size_t k=0;k<j;++k)v-=L[i][k]*L[j][k];
            if(i==j) {
                if(v<=0)throw std::invalid_argument("probability covariance not PSD");
                L[i][j]=std::sqrt(v);
            } else L[i][j]=v/L[j][j];
        }
    }
    const auto& scales=o.at("shock_scales").as_array();
    if(scales.size()!=6)throw std::invalid_argument("probability shock scales");
    for(std::size_t i=0;i<6;++i) {
        model.shock_scales[i]=number(scales[i]);
        if(model.shock_scales[i]<=0)throw std::invalid_argument("probability shock scale nonpositive");
    }
    model.uncertainty_z=number(o.at("uncertainty_z"));
    model.explicit_logit_reserve=number(o.at("explicit_logit_reserve"));
    model.execution_reserve_per_share=number(o.at("execution_reserve_per_share"));
    model.maximum_order_cost_microdollars=o.at("maximum_order_cost_microdollars").as_int64();
    model.maximum_quantity_microunits=o.at("maximum_quantity_microunits").as_int64();
    if(model.uncertainty_z<1.0 || model.uncertainty_z>10. || model.explicit_logit_reserve<0.
        || model.explicit_logit_reserve>10. || model.execution_reserve_per_share<0.
        || model.execution_reserve_per_share>=1. || model.maximum_order_cost_microdollars<=10
        || model.maximum_order_cost_microdollars>3'750'000 || model.maximum_quantity_microunits<=0
        || model.maximum_quantity_microunits>20'000'000)
        throw std::invalid_argument("probability experiment risk contract");
    unsigned char digest[EVP_MAX_MD_SIZE];unsigned int len=0;
    if(EVP_Digest(bytes.data(),bytes.size(),digest,&len,EVP_sha256(),nullptr)!=1 || len!=32)
        throw std::runtime_error("probability artifact SHA256 failure");
    std::ostringstream hex;
    for(unsigned int i=0;i<len;++i)hex<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned>(digest[i]);
    model.artifact_sha256=hex.str();model.version=1;model.loaded=true;return model;
}
bool probability_features(const NativeCryptoDecisionInput& in,std::string_view asset,
    std::string_view horizon,const std::array<double,6>& scales,
    std::array<double,kProbabilityFeatures>& x) noexcept {
    const auto a=std::find(kProbabilityAssets.begin(),kProbabilityAssets.end(),asset);
    const auto h=std::find(kProbabilityHorizons.begin(),kProbabilityHorizons.end(),horizon);
    if(a==kProbabilityAssets.end() || h==kProbabilityHorizons.end()
        || (in.signal.direction!=1 && in.signal.direction!=-1)
        || in.now_monotonic_ns<=0 || in.signal.trigger_receive_monotonic_ns<=0
        || in.signal.trigger_receive_monotonic_ns>in.now_monotonic_ns
        || in.market.close_monotonic_ns<=in.now_monotonic_ns
        || !std::isfinite(in.signal.binance_return_100ms_bp)
        || !std::isfinite(in.signal.coinbase_return_100ms_bp))return false;
    const auto& b=in.signal.direction>0?in.yes_book:in.no_book;
    const double scale=scales[static_cast<std::size_t>(a-kProbabilityAssets.begin())];
    const double age=static_cast<double>(in.now_monotonic_ns-in.signal.trigger_receive_monotonic_ns)/1e6;
    const double tte=static_cast<double>(in.market.close_monotonic_ns-in.now_monotonic_ns)/1e9;
    const double total=static_cast<double>(b.best_bid_microunits)+static_cast<double>(b.best_ask_microunits);
    if(!b.valid || !b.lineage_continuous || b.best_bid_e4<=0 || b.best_bid_e4>b.best_ask_e4
        || b.best_ask_e4>=10000 || b.best_bid_microunits<0 || b.best_ask_microunits<=0
        || !std::isfinite(scale) || scale<=0 || age<0 || tte<=0 || total<=0
        || b.receive_monotonic_ns<=0 || b.receive_monotonic_ns>in.now_monotonic_ns
        || in.now_monotonic_ns-b.receive_monotonic_ns>100'000'000LL
        || in.signal.trigger_receive_monotonic_ns<=0)return false;
    const double mid=std::clamp((static_cast<double>(b.best_bid_e4)+b.best_ask_e4)/20000.,.0001,.9999);
    x={};x[0]=1.;x[1]=std::log(mid/(1.-mid));
    x[2]=std::min(10.,std::abs(in.signal.binance_return_100ms_bp)/scale);
    x[3]=std::clamp(in.signal.direction*in.signal.coinbase_return_100ms_bp/scale,-10.,10.);
    x[4]=std::min(2.,std::log1p(age)/std::log(5001.));
    x[5]=std::clamp(std::log(tte/120.),-3.,3.);
    x[6]=std::min(20.,(b.best_ask_e4-b.best_bid_e4)/100.);
    x[7]=(static_cast<double>(b.best_bid_microunits)-b.best_ask_microunits)/total;
    x[8+static_cast<std::size_t>(a-kProbabilityAssets.begin())]=1.;
    if(h!=kProbabilityHorizons.begin())x[13+static_cast<std::size_t>(h-kProbabilityHorizons.begin())]=1.;
    return std::all_of(x.begin(),x.end(),[](double v){return std::isfinite(v);});
}
SettlementProbabilityForecast NativeProbabilityModel::predict(const NativeCryptoDecisionInput& in,
    std::string_view asset,std::string_view horizon) const noexcept {
    SettlementProbabilityForecast f;std::array<double,kProbabilityFeatures> x{};
    if(!loaded || !probability_features(in,asset,horizon,shock_scales,x))return f;
    double logit=0.,variance=0.;
    for(std::size_t i=0;i<x.size();++i) {
        logit+=x[i]*coefficients[i];
        for(std::size_t j=0;j<x.size();++j)variance+=x[i]*covariance[i][j]*x[j];
    }
    if(!std::isfinite(logit) || !std::isfinite(variance) || variance< -1e-8)return f;
    const double margin=uncertainty_z*std::sqrt(std::max(0.,variance))+explicit_logit_reserve;
    const double q=sigmoid(logit),lo=sigmoid(logit-margin),hi=sigmoid(logit+margin);
    const bool up=in.signal.direction>0;
    f.up=up?q:1.-q;f.lower=up?lo:1.-hi;f.upper=up?hi:1.-lo;
    f.asof_ns=in.now_monotonic_ns;
    const auto& b=up?in.yes_book:in.no_book;
    f.max_input_receive_ns=std::max(b.receive_monotonic_ns,in.signal.trigger_receive_monotonic_ns);
    if(f.asof_ns>std::numeric_limits<std::int64_t>::max()-100'000'000LL)return {};
    f.valid_until_ns=f.asof_ns+100'000'000LL;f.version=version;f.valid=1;
    f.forward_calibrated=0;return f;
}
} // namespace pm::v7
