#include "pm/api.hpp"
#include <boost/json.hpp>
#include <algorithm>
#include <cctype>
#include <sstream>
#include <stdexcept>

namespace pm {
namespace json = boost::json;
namespace {

double number(const json::value& v,double d=0.0){
    try{if(v.is_double())return v.as_double();if(v.is_int64())return static_cast<double>(v.as_int64());if(v.is_uint64())return static_cast<double>(v.as_uint64());if(v.is_string())return std::stod(std::string(v.as_string()));}catch(...){}
    return d;
}
std::string text(const json::value& v){
    if(v.is_string())return std::string(v.as_string());
    if(v.is_int64())return std::to_string(v.as_int64());
    if(v.is_uint64())return std::to_string(v.as_uint64());
    return {};
}
}

std::vector<MarketTrade> PolymarketApi::fetch_market_trades(
    const std::vector<std::string>& condition_ids,
    std::int64_t start_ts,
    std::int64_t end_ts) const {
    std::vector<MarketTrade> out;
    constexpr std::size_t batch_size=20;
    for(std::size_t pos=0;pos<condition_ids.size();pos+=batch_size){
        const auto end=std::min(condition_ids.size(),pos+batch_size);
        std::ostringstream markets;
        for(std::size_t i=pos;i<end;++i){if(i>pos)markets<<",";markets<<condition_ids[i];}
        std::ostringstream u;
        u<<cfg_.data_url<<"/trades?market="<<markets.str()<<"&start="<<std::max<std::int64_t>(0,start_ts)
         <<"&end="<<std::max<std::int64_t>(0,end_ts)<<"&limit=10000&takerOnly=true";
        auto r=http_.get(u.str());
        if(r.status<200||r.status>=300)throw std::runtime_error("Data API trades HTTP "+std::to_string(r.status)+": "+r.body.substr(0,300));
        auto root=json::parse(r.body);
        if(!root.is_array())throw std::runtime_error("Unexpected Data API trades response");
        for(auto const&v:root.as_array()){
            if(!v.is_object())continue;
            auto const&o=v.as_object();
            MarketTrade t;
            if(auto it=o.find("conditionId");it!=o.end())t.condition_id=text(it->value());
            if(auto it=o.find("asset");it!=o.end())t.asset=text(it->value());
            if(auto it=o.find("side");it!=o.end())t.side=text(it->value());
            if(auto it=o.find("size");it!=o.end())t.size=number(it->value());
            if(auto it=o.find("price");it!=o.end())t.price=number(it->value());
            if(auto it=o.find("timestamp");it!=o.end())t.timestamp=static_cast<std::int64_t>(number(it->value()));
            std::transform(t.side.begin(),t.side.end(),t.side.begin(),[](unsigned char c){return static_cast<char>(std::toupper(c));});
            if(!t.condition_id.empty()&&!t.asset.empty()&&t.size>0&&t.price>0&&t.price<1&&t.timestamp>0)out.push_back(std::move(t));
        }
    }
    std::sort(out.begin(),out.end(),[](auto const&a,auto const&b){return a.timestamp<b.timestamp;});
    return out;
}

} // namespace pm
