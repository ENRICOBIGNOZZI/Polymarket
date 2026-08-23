#include "pm/maker.hpp"

#include <boost/json.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iostream>
#include <limits>
#include <set>
#include <sstream>
#include <unordered_set>

namespace pm {
namespace fs = std::filesystem;
namespace json = boost::json;
namespace {

std::int64_t now_s(){
    return std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::vector<std::string> split_csv(const std::string& line){
    std::vector<std::string> out;
    std::string cur;
    bool quoted=false;
    for(std::size_t i=0;i<line.size();++i){
        const char c=line[i];
        if(c=='"'){
            if(quoted&&i+1<line.size()&&line[i+1]=='"'){cur.push_back('"');++i;}
            else quoted=!quoted;
        }else if(c==','&&!quoted){out.push_back(cur);cur.clear();}
        else cur.push_back(c);
    }
    out.push_back(cur);
    return out;
}

double jdouble(const json::object&o,const char*k,double d){
    auto it=o.find(k);if(it==o.end())return d;auto const&v=it->value();
    try{if(v.is_double())return v.as_double();if(v.is_int64())return static_cast<double>(v.as_int64());if(v.is_uint64())return static_cast<double>(v.as_uint64());if(v.is_string())return std::stod(std::string(v.as_string()));}catch(...){}
    return d;
}
std::size_t jsize(const json::object&o,const char*k,std::size_t d){return static_cast<std::size_t>(std::max(0.0,jdouble(o,k,static_cast<double>(d))));}
bool jbool(const json::object&o,const char*k,bool d){auto it=o.find(k);return it!=o.end()&&it->value().is_bool()?it->value().as_bool():d;}
std::string jstring(const json::object&o,const char*k,const std::string&d){auto it=o.find(k);return it!=o.end()&&it->value().is_string()?std::string(it->value().as_string()):d;}

struct LiteSignal{
    std::int64_t ts=0;
    std::string market_id;
    std::string slug;
    std::string side;
    double fair=0.5;
    double uncertainty=1.0;
};

std::vector<LiteSignal> latest_signals(const fs::path& path){
    std::ifstream f(path);
    if(!f)return{};
    std::string line;
    std::getline(f,line);
    std::vector<LiteSignal> all;
    std::int64_t latest=0;
    while(std::getline(f,line)){
        auto x=split_csv(line);
        if(x.size()<17)continue;
        try{
            LiteSignal s;
            s.ts=std::stoll(x[0]);s.market_id=x[1];s.slug=x[2];s.side=x[3];s.fair=std::stod(x[6]);s.uncertainty=std::stod(x[8]);
            latest=std::max(latest,s.ts);all.push_back(std::move(s));
        }catch(...){}
    }
    std::vector<LiteSignal> out;
    for(auto& s:all)if(s.ts==latest)out.push_back(std::move(s));
    return out;
}

} // namespace

MakerPaperController::MakerPaperController(Config cfg):cfg_(std::move(cfg)),api_(cfg_),cash_(cfg_.starting_capital),peak_equity_(cfg_.starting_capital){
    ensure_runtime();
    load_state();
}

void MakerPaperController::apply_config(Config& c,const std::string& path){
    std::ifstream f(path);if(!f)return;std::stringstream ss;ss<<f.rdbuf();boost::system::error_code ec;auto root=json::parse(ss.str(),ec);if(ec||!root.is_object())return;auto const&o=root.as_object();
    c.data_url=jstring(o,"data_url",c.data_url);
    c.maker_enabled=jbool(o,"maker_enabled",c.maker_enabled);
    c.maker_min_edge=jdouble(o,"maker_min_edge",c.maker_min_edge);
    c.maker_quote_usd=jdouble(o,"maker_quote_usd",c.maker_quote_usd);
    c.maker_fractional_kelly=jdouble(o,"maker_fractional_kelly",c.maker_fractional_kelly);
    c.maker_uncertainty_penalty=jdouble(o,"maker_uncertainty_penalty",c.maker_uncertainty_penalty);
    c.maker_adverse_spread_mult=jdouble(o,"maker_adverse_spread_mult",c.maker_adverse_spread_mult);
    c.maker_max_open_quotes=jsize(o,"maker_max_open_quotes",c.maker_max_open_quotes);
    c.maker_order_ttl_seconds=static_cast<std::int64_t>(jdouble(o,"maker_order_ttl_seconds",static_cast<double>(c.maker_order_ttl_seconds)));
    c.maker_min_fill_fraction=jdouble(o,"maker_min_fill_fraction",c.maker_min_fill_fraction);
    c.maker_improve_ticks=jsize(o,"maker_improve_ticks",c.maker_improve_ticks);
}

std::string MakerPaperController::csv_escape(const std::string&s){
    if(s.find_first_of(",\"\n")==std::string::npos)return s;
    std::string out="\"";for(char c:s){if(c=='\"')out+="\"\"";else out+=c;}return out+"\"";
}

double MakerPaperController::quote_price(const Book&book,std::size_t improve_ticks){
    const double bid=book.best_bid(),ask=book.best_ask();
    if(!std::isfinite(bid)||!std::isfinite(ask)||bid<=0||ask>=1||ask<=bid)return std::numeric_limits<double>::quiet_NaN();
    const double tick=std::max(1e-6,book.tick_size);
    double q=bid;
    if(improve_ticks>0&&ask-bid>2.0*tick+1e-9){
        q=std::min(bid+static_cast<double>(improve_ticks)*tick,ask-tick);
        q=std::max(q,bid);
    }
    if(q>=ask-1e-12)q=bid;
    return q;
}

double MakerPaperController::queue_ahead(const Book&book,double quote){
    const double eps=std::max(1e-9,book.tick_size*0.05);
    double q=0.0;
    for(const auto& level:book.bids)if(level.price>quote+eps||std::abs(level.price-quote)<=eps)q+=std::max(0.0,level.size);
    return q;
}

void MakerPaperController::ensure_runtime() const{
    fs::create_directories(cfg_.run_dir);
    auto ensure=[&](const std::string&name,const std::string&header){const auto p=fs::path(cfg_.run_dir)/name;if(!fs::exists(p)||fs::file_size(p)==0){std::ofstream f(p);f<<header<<'\n';}};
    ensure("maker_quotes.csv","timestamp,market_id,slug,side,bid,ask,quote,fair_side,uncertainty,maker_edge,queue_ahead");
    ensure("maker_orders.csv","market_id,condition_id,event_id,slug,side,token_id,quote,shares,queue_ahead,fair_at_submit,uncertainty_at_submit,created_ts,last_checked_ts");
}

void MakerPaperController::load_state(){
    {
        std::ifstream f(fs::path(cfg_.run_dir)/"risk_state.csv");std::string line;std::getline(f,line);if(std::getline(f,line)){auto x=split_csv(line);if(x.size()>=3){try{cash_=std::stod(x[0]);peak_equity_=std::stod(x[1]);killed_=std::stoi(x[2])!=0;}catch(...){}}}
    }
    {
        std::ifstream f(fs::path(cfg_.run_dir)/"broker_state.csv");std::string line;std::getline(f,line);while(std::getline(f,line)){auto x=split_csv(line);if(x.size()<9)continue;try{Position p{x[0],x[1],x[2],x[3],x[4],std::stod(x[5]),std::stod(x[6]),std::stod(x[7]),std::stod(x[8])};positions_[p.market_id]=std::move(p);}catch(...){}}
    }
    {
        std::ifstream f(fs::path(cfg_.run_dir)/"maker_orders.csv");std::string line;std::getline(f,line);while(std::getline(f,line)){auto x=split_csv(line);if(x.size()<13)continue;try{MakerOrder o;o.market_id=x[0];o.condition_id=x[1];o.event_id=x[2];o.slug=x[3];o.side=x[4];o.token_id=x[5];o.quote=std::stod(x[6]);o.shares=std::stod(x[7]);o.queue_ahead=std::stod(x[8]);o.fair_at_submit=std::stod(x[9]);o.uncertainty_at_submit=std::stod(x[10]);o.created_ts=std::stoll(x[11]);o.last_checked_ts=std::stoll(x[12]);if(o.quote>0&&o.quote<1&&o.shares>0)orders_[o.market_id]=std::move(o);}catch(...){}}
    }
}

void MakerPaperController::persist_state() const{
    {
        std::ofstream f(fs::path(cfg_.run_dir)/"maker_orders.csv");f<<"market_id,condition_id,event_id,slug,side,token_id,quote,shares,queue_ahead,fair_at_submit,uncertainty_at_submit,created_ts,last_checked_ts\n";
        for(const auto&[id,o]:orders_){(void)id;f<<csv_escape(o.market_id)<<','<<csv_escape(o.condition_id)<<','<<csv_escape(o.event_id)<<','<<csv_escape(o.slug)<<','<<o.side<<','<<o.token_id<<','<<o.quote<<','<<o.shares<<','<<o.queue_ahead<<','<<o.fair_at_submit<<','<<o.uncertainty_at_submit<<','<<o.created_ts<<','<<o.last_checked_ts<<'\n';}
    }
    {
        std::ofstream f(fs::path(cfg_.run_dir)/"broker_state.csv");f<<"market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid\n";
        for(const auto&[id,p]:positions_){(void)id;f<<csv_escape(p.market_id)<<','<<csv_escape(p.event_id)<<','<<csv_escape(p.slug)<<','<<p.side<<','<<p.token_id<<','<<p.shares<<','<<p.avg_price<<','<<p.cost_basis<<','<<p.fees_paid<<'\n';}
    }
    {
        std::ofstream f(fs::path(cfg_.run_dir)/"risk_state.csv");f<<"cash,peak_equity,killed\n"<<cash_<<','<<peak_equity_<<','<<(killed_?1:0)<<'\n';
    }
}

void MakerPaperController::append_quote(const MakerCandidate&c) const{
    std::ofstream f(fs::path(cfg_.run_dir)/"maker_quotes.csv",std::ios::app);f<<now_s()<<','<<csv_escape(c.market_id)<<','<<csv_escape(c.slug)<<','<<c.side<<','<<c.bid<<','<<c.ask<<','<<c.quote<<','<<c.fair_side<<','<<c.uncertainty<<','<<c.maker_edge<<','<<c.queue_ahead<<'\n';
}

void MakerPaperController::append_fill(const Fill&x) const{
    std::ofstream f(fs::path(cfg_.run_dir)/"fills.csv",std::ios::app);f<<x.ts<<','<<csv_escape(x.market_id)<<','<<csv_escape(x.slug)<<','<<x.action<<','<<x.side<<','<<x.shares<<','<<x.price<<','<<x.notional<<','<<x.fee<<'\n';
}

double MakerPaperController::reserved_notional() const{double x=0;for(const auto&[id,o]:orders_){(void)id;x+=o.quote*o.shares;}return x;}
double MakerPaperController::reserved_event(const std::string&e) const{double x=0;for(const auto&[id,o]:orders_){(void)id;if(o.event_id==e)x+=o.quote*o.shares;}return x;}
double MakerPaperController::event_exposure(const std::string&e) const{double x=0;for(const auto&[id,p]:positions_){(void)id;if(p.event_id==e)x+=p.cost_basis;}return x;}
double MakerPaperController::market_exposure(const std::string&m) const{auto it=positions_.find(m);return it==positions_.end()?0.0:it->second.cost_basis;}

std::size_t MakerPaperController::process_pending(bool allow_fill){
    if(!allow_fill||orders_.empty())return 0;
    const auto now=now_s();
    std::vector<std::string> erase;
    std::vector<std::string> conditions;
    std::int64_t start=now;
    for(const auto&[id,o]:orders_){if(now-o.created_ts>cfg_.maker_order_ttl_seconds){erase.push_back(id);continue;}conditions.push_back(o.condition_id);start=std::min(start,std::max(o.created_ts,o.last_checked_ts)+1);}
    for(const auto&id:erase)orders_.erase(id);
    if(orders_.empty()||conditions.empty()||start>now)return 0;
    std::sort(conditions.begin(),conditions.end());conditions.erase(std::unique(conditions.begin(),conditions.end()),conditions.end());
    std::vector<MarketTrade> trades;
    try{trades=api_.fetch_market_trades(conditions,start,now);}catch(const std::exception&e){std::cerr<<"maker trade lookup failed: "<<e.what()<<'\n';return 0;}
    std::size_t fills=0;erase.clear();
    for(auto&[id,o]:orders_){
        if(positions_.count(id)){erase.push_back(id);continue;}
        double crossed=0.0;
        for(const auto&t:trades){if(t.condition_id!=o.condition_id||t.asset!=o.token_id||t.side!="SELL")continue;if(t.timestamp<=o.last_checked_ts||t.timestamp<=o.created_ts)continue;if(t.price<=o.quote+1e-9)crossed+=t.size;}
        const double after_queue=std::max(0.0,crossed-o.queue_ahead);
        const double fill=std::min(o.shares,after_queue);
        o.queue_ahead=std::max(0.0,o.queue_ahead-crossed);o.last_checked_ts=now;
        if(fill+1e-12<cfg_.maker_min_fill_fraction*o.shares)continue;
        const double cost=fill*o.quote;if(cost>cash_){erase.push_back(id);continue;}
        positions_[id]=Position{o.market_id,o.event_id,o.slug,o.side,o.token_id,fill,o.quote,cost,0.0};cash_-=cost;append_fill({now,o.market_id,o.slug,"MAKER_BUY",o.side,fill,o.quote,cost,0.0});erase.push_back(id);++fills;
    }
    for(const auto&id:erase)orders_.erase(id);
    return fills;
}

std::vector<MakerCandidate> MakerPaperController::latest_candidates(const std::vector<Market>&markets,const std::unordered_map<std::string,Book>&books) const{
    auto signals=latest_signals(fs::path(cfg_.run_dir)/"signals.csv");
    std::unordered_map<std::string,const Market*> map;for(const auto&m:markets)map[m.id]=&m;
    std::vector<MakerCandidate> out;
    for(const auto&s:signals){
        auto mi=map.find(s.market_id);if(mi==map.end())continue;const auto&m=*mi->second;const std::string token=s.side=="YES"?m.yes_token:m.no_token;auto bi=books.find(token);if(bi==books.end())continue;const auto&b=bi->second;
        const double bid=b.best_bid(),ask=b.best_ask(),quote=quote_price(b,cfg_.maker_improve_ticks);if(!std::isfinite(bid)||!std::isfinite(ask)||!std::isfinite(quote)||quote>=ask)continue;
        const double edge=s.fair-quote-cfg_.maker_uncertainty_penalty*s.uncertainty-cfg_.maker_adverse_spread_mult*(ask-bid);
        out.push_back(MakerCandidate{m.id,m.condition_id,m.event_id,m.slug,s.side,token,bid,ask,quote,s.fair,s.uncertainty,edge,queue_ahead(b,quote),b.min_order_size});
    }
    return out;
}

std::size_t MakerPaperController::place_quotes(std::vector<MakerCandidate>candidates,double equity,double gross,bool allow_place){
    for(const auto&c:candidates)append_quote(c);
    if(!allow_place||!cfg_.maker_enabled||killed_||equity<=0)return 0;
    std::sort(candidates.begin(),candidates.end(),[](const auto&a,const auto&b){return a.maker_edge>b.maker_edge;});
    double reserved=reserved_notional();std::size_t placed=0;
    for(const auto&c:candidates){
        if(orders_.size()>=cfg_.maker_max_open_quotes)break;if(c.maker_edge<=cfg_.maker_min_edge||positions_.count(c.market_id)||orders_.count(c.market_id))continue;
        const double kelly=cfg_.maker_fractional_kelly*std::max(0.0,(c.fair_side-c.quote)/(1.0-c.quote))*equity;
        const double market_room=cfg_.max_market_fraction*equity-market_exposure(c.market_id);
        const double event_room=cfg_.max_event_fraction*equity-event_exposure(c.event_id)-reserved_event(c.event_id);
        const double gross_room=cfg_.max_gross_fraction*equity-gross-reserved;
        const double dd_room=cfg_.max_drawdown*peak_equity_-(peak_equity_-equity)-gross-reserved;
        const double cash_room=cash_-reserved;
        const double notional=std::min({cfg_.maker_quote_usd,kelly,market_room,event_room,gross_room,dd_room,cash_room});if(notional<=0)continue;
        const double shares=notional/c.quote;if(shares+1e-12<c.min_order_size)continue;const auto now=now_s();orders_[c.market_id]=MakerOrder{c.market_id,c.condition_id,c.event_id,c.slug,c.side,c.token_id,c.quote,shares,c.queue_ahead,c.fair_side,c.uncertainty,now,now};reserved+=notional;++placed;
    }
    return placed;
}

void MakerPaperController::run_once(bool allow_place_and_fill){
    const std::size_t fills=process_pending(allow_place_and_fill);
    auto markets=api_.discover_markets(cfg_.market_limit,cfg_.min_liquidity);
    auto signals=latest_signals(fs::path(cfg_.run_dir)/"signals.csv");std::unordered_set<std::string>ids;for(const auto&s:signals)ids.insert(s.market_id);
    std::vector<std::string>tokens;for(const auto&m:markets)if(ids.count(m.id)){tokens.push_back(m.yes_token);tokens.push_back(m.no_token);}auto books=tokens.empty()?std::unordered_map<std::string,Book>{}:api_.fetch_books(tokens);
    auto candidates=latest_candidates(markets,books);
    double equity=cash_,gross=0.0;
    try{std::ifstream f(fs::path(cfg_.run_dir)/"status.json");std::stringstream ss;ss<<f.rdbuf();auto v=json::parse(ss.str());if(v.is_object()){equity=jdouble(v.as_object(),"equity",equity);gross=jdouble(v.as_object(),"gross_exposure",gross);peak_equity_=jdouble(v.as_object(),"peak_equity",peak_equity_);killed_=jbool(v.as_object(),"killed",killed_);}}catch(...){}
    const std::size_t positive=static_cast<std::size_t>(std::count_if(candidates.begin(),candidates.end(),[&](const auto&c){return c.maker_edge>cfg_.maker_min_edge;}));double best=-1e9;for(const auto&c:candidates)best=std::max(best,c.maker_edge);
    const std::size_t placed=place_quotes(std::move(candidates),equity,gross,allow_place_and_fill);persist_state();
    std::ofstream st(fs::path(cfg_.run_dir)/"maker_status.json");st<<json::serialize(json::object{{"timestamp",now_s()},{"cash",cash_},{"pending_quotes",orders_.size()},{"fills_this_tick",fills},{"placed_this_tick",placed},{"positive_candidates",positive},{"best_maker_edge",std::isfinite(best)?best:0.0}})<<'\n';
    std::cout<<"maker positive="<<positive<<" best_edge="<<(std::isfinite(best)?best:0.0)<<" pending="<<orders_.size()<<" placed="<<placed<<" fills="<<fills<<'\n';
}

} // namespace pm
