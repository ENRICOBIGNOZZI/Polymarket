#include "pm/api.hpp"
#include <boost/json.hpp>
#include <algorithm>
#include <cctype>
#include <sstream>
#include <stdexcept>

namespace pm {
namespace json = boost::json;
namespace {
double as_double(const json::value& v, double d=0.0) {
    try {
        if (v.is_double()) return v.as_double();
        if (v.is_int64()) return static_cast<double>(v.as_int64());
        if (v.is_uint64()) return static_cast<double>(v.as_uint64());
        if (v.is_string()) return std::stod(std::string(v.as_string()));
    } catch (...) {}
    return d;
}
bool as_bool(const json::value& v, bool d=false) {
    if (v.is_bool()) return v.as_bool();
    if (v.is_int64()) return v.as_int64()!=0;
    if (v.is_string()) {
        auto s = std::string(v.as_string());
        std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});
        if (s=="true" || s=="1") return true;
        if (s=="false" || s=="0") return false;
    }
    return d;
}
std::string as_string(const json::value& v) {
    if (v.is_string()) return std::string(v.as_string());
    if (v.is_int64()) return std::to_string(v.as_int64());
    if (v.is_uint64()) return std::to_string(v.as_uint64());
    return {};
}
std::vector<std::string> string_array(const json::object& o, const char* key) {
    std::vector<std::string> out;
    auto it=o.find(key); if(it==o.end()) return out;
    json::value v=it->value();
    if (v.is_string()) {
        boost::system::error_code ec;
        auto parsed=json::parse(v.as_string(),ec);
        if (!ec) v=std::move(parsed); else { out.push_back(std::string(v.as_string())); return out; }
    }
    if (v.is_array()) for (auto const& x:v.as_array()) out.push_back(as_string(x));
    return out;
}
std::optional<int> resolution_from_market(const json::object& o, const std::vector<std::string>& outcomes) {
    auto it=o.find("outcomePrices");
    if (it==o.end()) return std::nullopt;
    json::value v=it->value();
    if (v.is_string()) { boost::system::error_code ec; auto p=json::parse(v.as_string(),ec); if(!ec) v=std::move(p); }
    if (!v.is_array() || v.as_array().size()<2 || outcomes.size()<2) return std::nullopt;
    const double p0=as_double(v.as_array()[0],-1), p1=as_double(v.as_array()[1],-1);
    if (std::max(p0,p1)<0.999) return std::nullopt;
    int win=p0>p1?0:1;
    std::string label=outcomes[win];
    std::transform(label.begin(),label.end(),label.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});
    if (label=="yes") return 1;
    if (label=="no") return 0;
    return std::nullopt;
}
std::optional<Market> parse_market_object(const json::object& o, double min_liquidity) {
    Market m;
    if(auto it=o.find("id");it!=o.end()) m.id=as_string(it->value());
    if(auto it=o.find("conditionId");it!=o.end()) m.condition_id=as_string(it->value());
    if(auto it=o.find("slug");it!=o.end()) m.slug=as_string(it->value());
    if(auto it=o.find("question");it!=o.end()) m.question=as_string(it->value());
    if(auto it=o.find("liquidityNum");it!=o.end()) m.liquidity=as_double(it->value());
    else if(auto it=o.find("liquidity");it!=o.end()) m.liquidity=as_double(it->value());
    if(auto it=o.find("negRisk");it!=o.end()) m.neg_risk=as_bool(it->value());
    if(auto it=o.find("active");it!=o.end()) m.active=as_bool(it->value(),true);
    if(auto it=o.find("closed");it!=o.end()) m.closed=as_bool(it->value());
    if(auto it=o.find("eventId");it!=o.end()) m.event_id=as_string(it->value());
    if(m.event_id.empty()) {
        auto it=o.find("events");
        if(it!=o.end()&&it->value().is_array()&&!it->value().as_array().empty()&&it->value().as_array()[0].is_object()) {
            auto const& eo=it->value().as_array()[0].as_object();
            auto ei=eo.find("id"); if(ei!=eo.end()) m.event_id=as_string(ei->value());
        }
    }
    if(m.event_id.empty()) m.event_id=m.condition_id.empty()?m.id:m.condition_id;
    auto tokens=string_array(o,"clobTokenIds");
    auto outcomes=string_array(o,"outcomes");
    if(tokens.size()<2) return std::nullopt;
    int yi=0, ni=1;
    for(std::size_t i=0;i<outcomes.size()&&i<tokens.size();++i) {
        auto s=outcomes[i]; std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});
        if(s=="yes") yi=static_cast<int>(i); else if(s=="no") ni=static_cast<int>(i);
    }
    m.yes_token=tokens[yi]; m.no_token=tokens[ni];
    m.resolved_yes=m.closed?resolution_from_market(o,outcomes):std::nullopt;
    if(auto it=o.find("feeSchedule");it!=o.end()&&it->value().is_object()) {
        auto const& fs=it->value().as_object();
        if(auto x=fs.find("rate");x!=fs.end()) m.fee_rate=as_double(x->value(),-1.0);
        if(auto x=fs.find("exponent");x!=fs.end()) m.fee_exponent=as_double(x->value(),1.0);
        if(auto x=fs.find("takerOnly");x!=fs.end()) m.fee_taker_only=as_bool(x->value(),true);
    } else if(auto it=o.find("feesEnabled");it!=o.end()&&!as_bool(it->value(),false)) {
        m.fee_rate=0.0;
    }
    if(m.id.empty()||m.condition_id.empty()||m.yes_token.empty()||m.no_token.empty()) return std::nullopt;
    if(m.liquidity+1e-12<min_liquidity) return std::nullopt;
    return m;
}
}

PolymarketApi::PolymarketApi(Config cfg): cfg_(std::move(cfg)) {}

std::vector<Market> PolymarketApi::discover_markets(std::size_t limit, double min_liquidity) const {
    limit=std::min<std::size_t>(limit,500);
    std::ostringstream u;
    u<<cfg_.gamma_url<<"/markets?active=true&closed=false&limit="<<limit<<"&order=liquidity&ascending=false";
    auto r=http_.get(u.str());
    if(r.status<200||r.status>=300) throw std::runtime_error("Gamma markets HTTP "+std::to_string(r.status)+": "+r.body.substr(0,300));
    auto root=json::parse(r.body);
    const json::array* arr=nullptr;
    if(root.is_array()) arr=&root.as_array();
    else if(root.is_object()) { auto it=root.as_object().find("markets"); if(it!=root.as_object().end()&&it->value().is_array()) arr=&it->value().as_array(); }
    if(!arr) throw std::runtime_error("Unexpected Gamma markets response");
    std::vector<Market> out; out.reserve(arr->size());
    for(auto const& v:*arr) if(v.is_object()) if(auto m=parse_market_object(v.as_object(),min_liquidity)) out.push_back(std::move(*m));
    return out;
}

std::optional<Market> PolymarketApi::fetch_market_by_id(const std::string& id) const {
    auto r=http_.get(cfg_.gamma_url+"/markets/"+id);
    if(r.status==404) return std::nullopt;
    if(r.status<200||r.status>=300) throw std::runtime_error("Gamma market HTTP "+std::to_string(r.status)+": "+r.body.substr(0,300));
    auto root=json::parse(r.body);
    if(!root.is_object()) return std::nullopt;
    return parse_market_object(root.as_object(),-1.0);
}

std::unordered_map<std::string,Book> PolymarketApi::fetch_books(const std::vector<std::string>& token_ids) const {
    std::unordered_map<std::string,Book> out;
    const std::size_t batch=std::max<std::size_t>(1,cfg_.books_batch_size);
    for(std::size_t pos=0;pos<token_ids.size();pos+=batch) {
        json::array body; const auto end=std::min(token_ids.size(),pos+batch);
        for(std::size_t i=pos;i<end;++i) body.emplace_back(json::object{{"token_id",token_ids[i]}});
        auto r=http_.post_json(cfg_.clob_url+"/books",json::serialize(body));
        if(r.status<200||r.status>=300) throw std::runtime_error("CLOB books HTTP "+std::to_string(r.status)+": "+r.body.substr(0,300));
        auto root=json::parse(r.body); if(!root.is_array()) throw std::runtime_error("Unexpected CLOB books response");
        for(auto const& v:root.as_array()) {
            if(!v.is_object()) continue;
            auto const& o=v.as_object(); Book b;
            if(auto it=o.find("asset_id");it!=o.end()) b.token_id=as_string(it->value());
            if(auto it=o.find("tick_size");it!=o.end()) b.tick_size=as_double(it->value(),0.01);
            if(auto it=o.find("min_order_size");it!=o.end()) b.min_order_size=as_double(it->value(),1.0);
            if(auto it=o.find("neg_risk");it!=o.end()) b.neg_risk=as_bool(it->value());
            if(auto it=o.find("last_trade_price");it!=o.end()) b.last_trade=as_double(it->value());
            auto parse_levels=[&](const char* key,std::vector<Level>& dst){ auto it=o.find(key); if(it==o.end()||!it->value().is_array()) return; for(auto const& lv:it->value().as_array()){ if(!lv.is_object()) continue; auto const& lo=lv.as_object(); auto p=lo.find("price"),s=lo.find("size"); if(p!=lo.end()&&s!=lo.end()){ double px=as_double(p->value(),-1), sz=as_double(s->value(),0); if(px>0&&px<1&&sz>0) dst.push_back({px,sz}); } } };
            parse_levels("bids",b.bids); parse_levels("asks",b.asks);
            if(!b.token_id.empty()) out[b.token_id]=std::move(b);
        }
    }
    return out;
}

FeeDetails PolymarketApi::fetch_fee_details(const Market& market) const {
    if(market.fee_rate>=0.0) return FeeDetails{market.fee_rate,market.fee_exponent,market.fee_taker_only};
    FeeDetails fd{0.07,1.0,true};
    try {
        auto r=http_.get(cfg_.clob_url+"/clob-markets/"+market.condition_id);
        if(r.status>=200&&r.status<300) {
            auto root=json::parse(r.body);
            if(root.is_object()) {
                auto it=root.as_object().find("fd");
                if(it!=root.as_object().end()&&it->value().is_object()) {
                    auto const& f=it->value().as_object();
                    if(auto x=f.find("r");x!=f.end()) fd.rate=as_double(x->value(),0.0);
                    if(auto x=f.find("e");x!=f.end()) fd.exponent=as_double(x->value(),1.0);
                    if(auto x=f.find("to");x!=f.end()) fd.taker_only=as_bool(x->value(),true);
                    return fd;
                }
            }
        }
    } catch(...) {}
    try {
        auto r=http_.get(cfg_.clob_url+"/fee-rate?token_id="+market.yes_token);
        if(r.status>=200&&r.status<300) {
            auto root=json::parse(r.body);
            if(root.is_object()) { auto it=root.as_object().find("base_fee"); if(it!=root.as_object().end()) fd.rate=as_double(it->value())/10000.0; }
        }
    } catch(...) {}
    return fd;
}

} // namespace pm
