#include "pm/engine.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <set>
#include <sstream>

namespace pm {
namespace fs = std::filesystem;
namespace {

std::int64_t maker_now_s(){
    return std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::vector<std::string> maker_csv_split(const std::string& line){
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

} // namespace

double Engine::maker_quote_price(const Book& book,std::size_t improve_ticks){
    const double bid=book.best_bid(),ask=book.best_ask();
    if(!std::isfinite(bid)||!std::isfinite(ask)||bid<=0||ask>=1||ask<=bid)return std::numeric_limits<double>::quiet_NaN();
    const double tick=std::max(1e-6,book.tick_size);
    double quote=bid;
    if(improve_ticks>0&&ask-bid>2.0*tick+1e-9){
        quote=std::min(bid+static_cast<double>(improve_ticks)*tick,ask-tick);
        quote=std::max(quote,bid);
    }
    if(quote>=ask-1e-12)quote=bid;
    return quote;
}

double Engine::maker_queue_ahead(const Book& book,double quote){
    double q=0.0;
    const double eps=std::max(1e-9,book.tick_size*0.05);
    for(const auto& level:book.bids){
        if(level.price>quote+eps||std::abs(level.price-quote)<=eps)q+=std::max(0.0,level.size);
    }
    return q;
}

void Engine::ensure_maker_runtime(){
    auto ensure=[&](const std::string& name,const std::string& header){
        const auto p=fs::path(cfg_.run_dir)/name;
        if(!fs::exists(p)||fs::file_size(p)==0){std::ofstream f(p);f<<header<<'\n';}
    };
    ensure("maker_quotes.csv","timestamp,market_id,slug,side,bid,ask,quote,fair_side,uncertainty,maker_edge,queue_ahead");
    ensure("maker_orders.csv","market_id,condition_id,event_id,slug,side,token_id,quote,shares,queue_ahead,fair_at_submit,uncertainty_at_submit,created_ts,last_checked_ts");
}

void Engine::load_maker_state(){
    std::ifstream f(fs::path(cfg_.run_dir)/"maker_orders.csv");
    if(!f)return;
    std::string line;
    std::getline(f,line);
    while(std::getline(f,line)){
        auto x=maker_csv_split(line);
        if(x.size()<13)continue;
        try{
            MakerOrder o;
            o.market_id=x[0];o.condition_id=x[1];o.event_id=x[2];o.slug=x[3];o.side=x[4];o.token_id=x[5];
            o.quote=std::stod(x[6]);o.shares=std::stod(x[7]);o.queue_ahead=std::stod(x[8]);o.fair_at_submit=std::stod(x[9]);
            o.uncertainty_at_submit=std::stod(x[10]);o.created_ts=std::stoll(x[11]);o.last_checked_ts=std::stoll(x[12]);
            if(o.quote>0&&o.quote<1&&o.shares>0&&!o.market_id.empty())maker_orders_[o.market_id]=std::move(o);
        }catch(...){}
    }
}

void Engine::persist_maker_state() const{
    std::ofstream f(fs::path(cfg_.run_dir)/"maker_orders.csv");
    f<<"market_id,condition_id,event_id,slug,side,token_id,quote,shares,queue_ahead,fair_at_submit,uncertainty_at_submit,created_ts,last_checked_ts\n";
    for(const auto& [id,o]:maker_orders_){
        (void)id;
        f<<csv_escape(o.market_id)<<','<<csv_escape(o.condition_id)<<','<<csv_escape(o.event_id)<<','<<csv_escape(o.slug)<<','
         <<o.side<<','<<o.token_id<<','<<o.quote<<','<<o.shares<<','<<o.queue_ahead<<','<<o.fair_at_submit<<','
         <<o.uncertainty_at_submit<<','<<o.created_ts<<','<<o.last_checked_ts<<'\n';
    }
}

void Engine::append_maker_candidate(const MakerCandidate& c) const{
    std::ofstream f(fs::path(cfg_.run_dir)/"maker_quotes.csv",std::ios::app);
    f<<maker_now_s()<<','<<csv_escape(c.market_id)<<','<<csv_escape(c.slug)<<','<<c.side<<','<<c.bid<<','<<c.ask<<','<<c.quote<<','
     <<c.fair_side<<','<<c.uncertainty<<','<<c.maker_edge<<','<<c.queue_ahead<<'\n';
}

double Engine::maker_reserved_notional() const{
    double x=0.0;
    for(const auto& [id,o]:maker_orders_){(void)id;x+=o.quote*o.shares;}
    return x;
}

double Engine::maker_reserved_event(const std::string& event_id) const{
    double x=0.0;
    for(const auto& [id,o]:maker_orders_){(void)id;if(o.event_id==event_id)x+=o.quote*o.shares;}
    return x;
}

void Engine::process_maker_orders(const std::unordered_map<std::string,Book>& books,bool allow_fill){
    if(!cfg_.maker_enabled||maker_orders_.empty()||!allow_fill)return;
    const auto now=maker_now_s();
    std::vector<std::string> expired;
    std::vector<std::string> conditions;
    std::int64_t start=now;
    for(const auto& [id,o]:maker_orders_){
        if(now-o.created_ts>cfg_.maker_order_ttl_seconds){expired.push_back(id);continue;}
        conditions.push_back(o.condition_id);
        start=std::min(start,std::max(o.created_ts,o.last_checked_ts)+1);
    }
    for(const auto& id:expired)maker_orders_.erase(id);
    if(maker_orders_.empty()||conditions.empty()||start>now)return;
    std::sort(conditions.begin(),conditions.end());
    conditions.erase(std::unique(conditions.begin(),conditions.end()),conditions.end());

    std::vector<MarketTrade> trades;
    try{trades=api_.fetch_market_trades(conditions,start,now);}catch(const std::exception& e){
        std::cerr<<"maker trade lookup failed: "<<e.what()<<'\n';
        return;
    }

    std::vector<std::string> done;
    for(auto& [id,o]:maker_orders_){
        if(positions_.count(id)){done.push_back(id);continue;}
        double crossed=0.0;
        for(const auto& t:trades){
            if(t.condition_id!=o.condition_id||t.asset!=o.token_id||t.side!="SELL")continue;
            if(t.timestamp<=o.last_checked_ts||t.timestamp<=o.created_ts)continue;
            if(t.price<=o.quote+1e-9)crossed+=t.size;
        }
        const double after_queue=std::max(0.0,crossed-o.queue_ahead);
        const double fill=std::min(o.shares,after_queue);
        o.queue_ahead=std::max(0.0,o.queue_ahead-crossed);
        o.last_checked_ts=now;
        if(fill+1e-12<cfg_.maker_min_fill_fraction*o.shares)continue;
        const double cost=fill*o.quote;
        if(cost>cash_){done.push_back(id);continue;}
        positions_[id]=Position{o.market_id,o.event_id,o.slug,o.side,o.token_id,fill,o.quote,cost,0.0};
        cash_-=cost;
        append_fill({now,o.market_id,o.slug,"MAKER_BUY",o.side,fill,o.quote,cost,0.0});
        done.push_back(id);
    }
    for(const auto& id:done)maker_orders_.erase(id);
}

void Engine::place_maker_orders(std::vector<MakerCandidate> candidates,double eq,double gross,bool allow_place){
    for(const auto& c:candidates)append_maker_candidate(c);
    if(!cfg_.maker_enabled||!allow_place||killed_||eq<=0)return;
    std::sort(candidates.begin(),candidates.end(),[](const auto&a,const auto&b){return a.maker_edge>b.maker_edge;});
    std::set<std::string> used_events;
    for(const auto& [id,o]:maker_orders_){(void)id;used_events.insert(o.event_id);}
    double reserved=maker_reserved_notional();
    for(const auto& c:candidates){
        if(maker_orders_.size()>=cfg_.maker_max_open_quotes)break;
        if(c.maker_edge<=cfg_.maker_min_edge||c.quote<=0||c.quote>=1)continue;
        if(positions_.count(c.market_id)||maker_orders_.count(c.market_id))continue;
        const double kelly=cfg_.maker_fractional_kelly*full_kelly(c.fair_side,c.quote)*eq;
        const double market_room=cfg_.max_market_fraction*eq-market_exposure(c.market_id);
        const double event_room=cfg_.max_event_fraction*eq-event_exposure(c.event_id)-maker_reserved_event(c.event_id);
        const double gross_room=cfg_.max_gross_fraction*eq-gross-reserved;
        const double cash_room=cash_-reserved;
        double notional=std::min({cfg_.maker_quote_usd,kelly,market_room,event_room,gross_room,cash_room});
        if(notional<=0)continue;
        const double shares=notional/c.quote;
        if(shares+1e-12<c.min_order_size)continue;
        const auto now=maker_now_s();
        maker_orders_[c.market_id]=MakerOrder{c.market_id,c.condition_id,c.event_id,c.slug,c.side,c.token_id,c.quote,shares,c.queue_ahead,c.fair_side,c.uncertainty,now,now};
        reserved+=notional;
    }
}

} // namespace pm
