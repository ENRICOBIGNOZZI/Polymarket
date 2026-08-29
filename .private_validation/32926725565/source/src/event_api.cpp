#include "pm/api.hpp"
#include <boost/json.hpp>
#include <algorithm>
#include <cctype>
#include <stdexcept>

namespace pm {
namespace json = boost::json;
namespace {

double number(const json::value& v,double d=0.0){
    try{if(v.is_double())return v.as_double();if(v.is_int64())return static_cast<double>(v.as_int64());if(v.is_uint64())return static_cast<double>(v.as_uint64());if(v.is_string())return std::stod(std::string(v.as_string()));}catch(...){}
    return d;
}
bool boolean(const json::value&v,bool d=false){
    if(v.is_bool())return v.as_bool();
    if(v.is_int64())return v.as_int64()!=0;
    if(v.is_string()){auto s=std::string(v.as_string());std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});if(s=="true"||s=="1")return true;if(s=="false"||s=="0")return false;}
    return d;
}
std::string text(const json::value&v){if(v.is_string())return std::string(v.as_string());if(v.is_int64())return std::to_string(v.as_int64());if(v.is_uint64())return std::to_string(v.as_uint64());return{};}
std::vector<std::string> array_strings(const json::object&o,const char*key){
    std::vector<std::string> out;auto it=o.find(key);if(it==o.end())return out;json::value v=it->value();
    if(v.is_string()){boost::system::error_code ec;auto p=json::parse(v.as_string(),ec);if(!ec)v=std::move(p);}
    if(v.is_array())for(auto const&x:v.as_array())out.push_back(text(x));return out;
}
std::optional<Market> parse_event_market(const json::object&o,const std::string&event_id){
    Market m;m.event_id=event_id;m.neg_risk=true;
    if(auto it=o.find("id");it!=o.end())m.id=text(it->value());
    if(auto it=o.find("conditionId");it!=o.end())m.condition_id=text(it->value());
    if(auto it=o.find("slug");it!=o.end())m.slug=text(it->value());
    if(auto it=o.find("question");it!=o.end())m.question=text(it->value());
    if(auto it=o.find("liquidityNum");it!=o.end())m.liquidity=number(it->value());
    if(auto it=o.find("volume24hr");it!=o.end())m.volume24h=number(it->value());
    if(auto it=o.find("active");it!=o.end())m.active=boolean(it->value(),true);
    if(auto it=o.find("closed");it!=o.end())m.closed=boolean(it->value(),false);
    if(auto it=o.find("enableOrderBook");it!=o.end())m.enable_order_book=boolean(it->value(),true);
    if(auto it=o.find("acceptingOrders");it!=o.end())m.accepting_orders=boolean(it->value(),true);
    auto tokens=array_strings(o,"clobTokenIds"), outcomes=array_strings(o,"outcomes");
    if(tokens.size()<2)return std::nullopt;int yi=0,ni=1;
    for(std::size_t i=0;i<outcomes.size()&&i<tokens.size();++i){auto s=outcomes[i];std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});if(s=="yes")yi=static_cast<int>(i);else if(s=="no")ni=static_cast<int>(i);}
    m.yes_token=tokens[yi];m.no_token=tokens[ni];
    if(auto it=o.find("feeSchedule");it!=o.end()&&it->value().is_object()){auto const&f=it->value().as_object();if(auto x=f.find("rate");x!=f.end())m.fee_rate=number(x->value(),-1.0);if(auto x=f.find("exponent");x!=f.end())m.fee_exponent=number(x->value(),1.0);if(auto x=f.find("takerOnly");x!=f.end())m.fee_taker_only=boolean(x->value(),true);}
    if(m.id.empty()||m.condition_id.empty()||m.yes_token.empty()||m.no_token.empty())return std::nullopt;
    if(!m.active||m.closed||!m.enable_order_book)return std::nullopt;
    return m;
}
}

std::vector<Market> PolymarketApi::fetch_event_markets(const std::string&event_id) const{
    auto r=http_.get(cfg_.gamma_url+"/events/"+event_id);
    if(r.status<200||r.status>=300)throw std::runtime_error("Gamma event HTTP "+std::to_string(r.status)+": "+r.body.substr(0,300));
    auto root=json::parse(r.body);if(!root.is_object())throw std::runtime_error("Unexpected Gamma event response");auto const&o=root.as_object();
    auto ni=o.find("negRisk");if(ni!=o.end()&&!boolean(ni->value(),false))return{};
    auto it=o.find("markets");if(it==o.end()||!it->value().is_array())return{};
    std::vector<Market>out;for(auto const&v:it->value().as_array())if(v.is_object())if(auto m=parse_event_market(v.as_object(),event_id))out.push_back(std::move(*m));return out;
}

} // namespace pm
