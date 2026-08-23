#include "poly/engine.hpp"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <curl/curl.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <utility>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>

namespace poly {
namespace {
using boost::property_tree::ptree;

size_t curl_write(void* data,size_t size,size_t nmemb,void* user){
    const auto n=size*nmemb; static_cast<std::string*>(user)->append(static_cast<char*>(data),n); return n;
}
double as_double(const ptree& p,const std::string& k,double d=0.0){
    if(auto x=p.get_optional<double>(k)) return *x;
    if(auto x=p.get_optional<std::string>(k)) try{return std::stod(*x);}catch(...){}
    return d;
}
bool as_bool(const ptree& p,const std::string& k,bool d=false){
    if(auto x=p.get_optional<bool>(k)) return *x;
    if(auto x=p.get_optional<std::string>(k)){
        std::string s=*x; std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return std::tolower(c);});
        if(s=="true"||s=="1") return true; if(s=="false"||s=="0") return false;
    }
    return d;
}
std::vector<std::string> parse_string_array(const std::string& raw){
    std::vector<std::string> out; if(raw.empty()) return out;
    try{ std::istringstream is(raw); ptree a; boost::property_tree::read_json(is,a); for(const auto& x:a) out.push_back(x.second.get_value<std::string>()); }catch(...){}
    return out;
}
std::vector<std::string> array_strings(const ptree& p,const std::string& k){
    if(auto c=p.get_child_optional(k)){
        if(!c->empty()){ std::vector<std::string> out; for(const auto& x:*c) out.push_back(x.second.get_value<std::string>()); return out; }
        if(!c->data().empty()) return parse_string_array(c->data());
    }
    if(auto s=p.get_optional<std::string>(k)) return parse_string_array(*s);
    return {};
}
std::string event_id(const ptree& p){
    if(auto es=p.get_child_optional("events")) for(const auto& e:*es){
        if(auto s=e.second.get_optional<std::string>("id")) return *s;
        if(auto n=e.second.get_optional<long long>("id")) return std::to_string(*n);
    }
    return p.get<std::string>("eventId","");
}
std::string now_iso(){
    const auto n=std::chrono::system_clock::now(); const auto t=std::chrono::system_clock::to_time_t(n); std::tm tm{};
#ifdef _WIN32
    gmtime_s(&tm,&t);
#else
    gmtime_r(&t,&tm);
#endif
    std::ostringstream os; os<<std::put_time(&tm,"%Y-%m-%dT%H:%M:%SZ"); return os.str();
}
double wmean(const std::vector<std::pair<double,double>>& x,double f){ double n=0,d=0; for(auto [a,w]:x){n+=a*w;d+=w;} return d>0?n/d:f; }
double logit(double p){p=std::clamp(p,.001,.999);return std::log(p/(1-p));}
}

HttpClient::HttpClient(){ static const int once=[](){curl_global_init(CURL_GLOBAL_DEFAULT);return 1;}();(void)once; }
HttpClient::~HttpClient()=default;
std::string HttpClient::get(const std::string& url) const{
    CURL* c=curl_easy_init(); if(!c) throw std::runtime_error("curl init failed"); std::string body;
    curl_easy_setopt(c,CURLOPT_URL,url.c_str()); curl_easy_setopt(c,CURLOPT_WRITEFUNCTION,curl_write); curl_easy_setopt(c,CURLOPT_WRITEDATA,&body);
    curl_easy_setopt(c,CURLOPT_USERAGENT,"PolymarketQuantEngine/0.3"); curl_easy_setopt(c,CURLOPT_TIMEOUT,15L); curl_easy_setopt(c,CURLOPT_CONNECTTIMEOUT,8L); curl_easy_setopt(c,CURLOPT_FOLLOWLOCATION,1L);
    const auto rc=curl_easy_perform(c); long status=0; curl_easy_getinfo(c,CURLINFO_RESPONSE_CODE,&status); curl_easy_cleanup(c);
    if(rc!=CURLE_OK) throw std::runtime_error(std::string("HTTP failure: ")+curl_easy_strerror(rc));
    if(status<200||status>=300) throw std::runtime_error("HTTP status "+std::to_string(status)+" for "+url); return body;
}

PolymarketClient::PolymarketClient(HttpClient h):http_(std::move(h)){}
std::vector<Market> parse_gamma_markets_json(const std::string& body){
    std::istringstream is(body); ptree root; boost::property_tree::read_json(is,root); std::vector<Market> out;
    for(const auto& it:root){ const auto& p=it.second; Market m;
        m.id=p.get<std::string>("id",""); m.question=p.get<std::string>("question",""); m.slug=p.get<std::string>("slug",""); m.condition_id=p.get<std::string>("conditionId",""); m.event_id=event_id(p); m.end_date=p.get<std::string>("endDate","");
        m.active=as_bool(p,"active"); m.closed=as_bool(p,"closed"); m.accepting_orders=as_bool(p,"acceptingOrders",true); m.neg_risk=as_bool(p,"negRisk");
        m.volume_24h=as_double(p,"volume24hr",as_double(p,"volume24hrClob")); m.liquidity=as_double(p,"liquidityNum",as_double(p,"liquidity"));
        const auto tok=array_strings(p,"clobTokenIds"), pr=array_strings(p,"outcomePrices"); if(tok.size()>=2){m.yes_token=tok[0];m.no_token=tok[1];}
        if(!pr.empty()) try{m.gamma_yes_price=std::stod(pr[0]);}catch(...){}
        if(!m.id.empty()&&!m.yes_token.empty()&&!m.no_token.empty()) out.push_back(std::move(m));
    }
    return out;
}
std::vector<Market> PolymarketClient::discover_markets(std::size_t limit) const{
    return parse_gamma_markets_json(http_.get("https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume24hr&ascending=false&limit="+std::to_string(limit)));
}
BookSnapshot PolymarketClient::get_book(const std::string& token) const{
    std::istringstream is(http_.get("https://clob.polymarket.com/book?token_id="+token)); ptree root; boost::property_tree::read_json(is,root); BookSnapshot b; b.token_id=token;
    if(auto xs=root.get_child_optional("bids")) for(const auto& x:*xs){Level l{as_double(x.second,"price"),as_double(x.second,"size")};if(l.price>0&&l.size>0)b.bids.push_back(l);}
    if(auto xs=root.get_child_optional("asks")) for(const auto& x:*xs){Level l{as_double(x.second,"price"),as_double(x.second,"size")};if(l.price>0&&l.size>0)b.asks.push_back(l);}
    for(const auto& l:b.bids)b.best_bid=std::max(b.best_bid,l.price); for(const auto& l:b.asks)b.best_ask=std::min(b.best_ask,l.price);
    for(const auto& l:b.bids)if(b.best_bid-l.price<=.0300001)b.bid_depth+=l.size; for(const auto& l:b.asks)if(l.price-b.best_ask<=.0300001)b.ask_depth+=l.size; return b;
}
std::optional<double> PolymarketClient::get_fee_rate(const std::string& condition) const{
    if(condition.empty()) return std::nullopt;
    try{ std::istringstream is(http_.get("https://clob.polymarket.com/clob-markets/"+condition)); ptree p; boost::property_tree::read_json(is,p);
        if(auto x=p.get_optional<double>("fd.r")) return std::max(0.0,*x); if(auto x=p.get_optional<std::string>("fd.r")) try{return std::max(0.0,std::stod(*x));}catch(...){}
        if(!p.get_child_optional("fd")) return 0.0;
    }catch(...){} return std::nullopt;
}
std::vector<LiveMarket> PolymarketClient::snapshot(std::size_t limit,double min_liq,double max_spread) const{
    std::vector<LiveMarket> out; for(auto& m:discover_markets(limit)){
        if(!m.accepting_orders||m.liquidity<min_liq)continue; try{auto y=get_book(m.yes_token),n=get_book(m.no_token); if(auto f=get_fee_rate(m.condition_id))m.fee_rate=*f;
        if(y.bids.empty()||y.asks.empty()||n.bids.empty()||n.asks.empty())continue; if(y.spread()<=0||y.spread()>max_spread||n.spread()<=0||n.spread()>max_spread)continue;
        out.push_back({m,std::move(y),std::move(n),hash_text_embedding(m.question)});}catch(const std::exception& e){std::cerr<<"[warn] market "<<m.id<<": "<<e.what()<<'\n';}
    } return out;
}

void ExternalSignalStore::load_csv(const std::string& path){ std::ifstream in(path); if(!in)throw std::runtime_error("cannot open external signal CSV: "+path); std::string s;std::getline(in,s);
    while(std::getline(in,s)){if(s.empty()||s[0]=='#')continue;std::stringstream ss(s);std::string id,p,c,src;std::getline(ss,id,',');std::getline(ss,p,',');std::getline(ss,c,',');std::getline(ss,src);try{set(id,{clamp_probability(std::stod(p)),std::clamp(std::stod(c),0.0,1.0),src,std::chrono::system_clock::now()});}catch(...){}}
}
void ExternalSignalStore::set(const std::string& id,ExternalSignal s){signals_[id]=std::move(s);} std::optional<ExternalSignal> ExternalSignalStore::get(const std::string& id) const{auto i=signals_.find(id);return i==signals_.end()?std::nullopt:std::optional<ExternalSignal>(i->second);}

std::vector<double> hash_text_embedding(const std::string& text,std::size_t dim){std::vector<double> v(dim);std::string t;auto flush=[&]{if(t.size()<2){t.clear();return;}uint64_t h=1469598103934665603ULL;for(unsigned char c:t){h^=c;h*=1099511628211ULL;}v[h%dim]+=((h>>8)&1)?1.0:-1.0;t.clear();};
    for(unsigned char c:text){if(std::isalnum(c))t.push_back(static_cast<char>(std::tolower(c)));else flush();}flush();double n=std::sqrt(std::inner_product(v.begin(),v.end(),v.begin(),0.0));if(n>0)for(auto&x:v)x/=n;return v;}
double cosine_similarity(const std::vector<double>& a,const std::vector<double>& b){return a.size()==b.size()&&!a.empty()?std::inner_product(a.begin(),a.end(),b.begin(),0.0):0.0;} double clamp_probability(double p){return std::clamp(p,.001,.999);}

UniversalModel::UniversalModel(EngineConfig c):cfg_(c){}
ExpertPrediction UniversalModel::microstructure(const LiveMarket& m) const{const auto& b=m.yes_book;if(b.bids.empty()||b.asks.empty())return{"micro",.5,0,false};double d=b.bid_depth+b.ask_depth,imb=d>0?(b.bid_depth-b.ask_depth)/d:0;double fair=clamp_probability(b.midpoint()+.35*b.spread()*imb);double sc=std::clamp(1-b.spread()/cfg_.max_spread,0.0,1.0),dc=std::clamp(std::log1p(d)/std::log(10001.0),0.0,1.0);return{"micro",fair,.25+.55*sc*dc,true};}
ExpertPrediction UniversalModel::factor(const LiveMarket& m,const std::vector<LiveMarket>& u) const{
    std::vector<const LiveMarket*> a;std::size_t common=std::numeric_limits<std::size_t>::max();for(const auto&x:u){auto i=price_history_.find(x.market.id);if(i==price_history_.end()||i->second.size()<cfg_.min_factor_history)continue;a.push_back(&x);common=std::min(common,i->second.size());}
    if(a.size()<3||common<cfg_.min_factor_history)return{"pca_factor",.5,0,false};common=std::min(common,cfg_.factor_history);const std::size_t n=a.size(),k=common-1;std::size_t target=n;std::vector<std::vector<double>> r(n,std::vector<double>(k));std::vector<double> mu(n);
    for(std::size_t i=0;i<n;++i){if(a[i]->market.id==m.market.id)target=i;const auto&h=price_history_.at(a[i]->market.id);auto off=h.size()-common;for(std::size_t t=0;t<k;++t){r[i][t]=logit(h[off+t+1])-logit(h[off+t]);mu[i]+=r[i][t];}mu[i]/=k;for(auto&x:r[i])x-=mu[i];}if(target==n)return{"pca_factor",.5,0,false};
    std::vector<std::vector<double>> C(n,std::vector<double>(n));for(std::size_t i=0;i<n;++i)for(std::size_t j=i;j<n;++j){for(std::size_t t=0;t<k;++t)C[i][j]+=r[i][t]*r[j][t];C[i][j]/=std::max<std::size_t>(1,k-1);C[j][i]=C[i][j];}
    std::vector<double> v(n,1/std::sqrt(double(n)));for(int z=0;z<30;++z){std::vector<double>w(n);for(std::size_t i=0;i<n;++i)for(std::size_t j=0;j<n;++j)w[i]+=C[i][j]*v[j];double q=std::sqrt(std::inner_product(w.begin(),w.end(),w.begin(),0.0));if(q<1e-12)return{"pca_factor",.5,0,false};for(std::size_t i=0;i<n;++i)v[i]=w[i]/q;}
    std::vector<double> cr(n);for(std::size_t i=0;i<n;++i){const auto&h=price_history_.at(a[i]->market.id);cr[i]=logit(a[i]->yes_book.midpoint())-logit(h.back())-mu[i];}double f=std::inner_product(v.begin(),v.end(),cr.begin(),0.0),res=cr[target]-v[target]*f,rss=0;
    for(std::size_t t=0;t<k;++t){double ft=0;for(std::size_t i=0;i<n;++i)ft+=v[i]*r[i][t];double e=r[target][t]-v[target]*ft;rss+=e*e;}double sd=std::sqrt(rss/std::max<std::size_t>(1,k-1)+1e-10),z=std::abs(res)/sd;double fl=logit(m.yes_book.midpoint())-.35*res,fair=1/(1+std::exp(-fl));double breadth=std::clamp(double(n)/20,0.0,1.0),conf=(.12+.38*std::clamp(z/3,0.0,1.0))*breadth;return{"pca_factor",clamp_probability(fair),conf,true};
}
ExpertPrediction UniversalModel::graph(const LiveMarket& m,const std::vector<LiveMarket>& u) const{if(!m.market.neg_risk||m.market.event_id.empty())return{"graph",.5,0,false};std::vector<const LiveMarket*>g;for(const auto&x:u)if(x.market.neg_risk&&x.market.event_id==m.market.event_id)g.push_back(&x);if(g.size()<2)return{"graph",.5,0,false};double s=0;for(auto*x:g)s+=x->yes_book.midpoint();if(s<=0)return{"graph",.5,0,false};return{"graph",clamp_probability(m.yes_book.midpoint()/s),.30+.50*std::min(1.0,std::abs(s-1)),true};}
ExpertPrediction UniversalModel::semantic_relative_value(const LiveMarket& m,const std::vector<LiveMarket>& u) const{std::vector<std::pair<double,double>>n;for(const auto&x:u)if(x.market.id!=m.market.id){double s=cosine_similarity(m.text_embedding,x.text_embedding);if(s>=.72)n.emplace_back(x.yes_book.midpoint(),s*s);}if(n.size()<2)return{"semantic_rv",.5,0,false};double cur=m.yes_book.midpoint();return{"semantic_rv",clamp_probability(.85*cur+.15*wmean(n,cur)),std::min(.30,.08+.03*n.size()),true};}
ExpertPrediction UniversalModel::external_signal(const LiveMarket& m,const ExternalSignalStore& e) const{auto s=e.get(m.market.id);if(!s)return{"external",.5,0,false};auto age=std::chrono::duration_cast<std::chrono::hours>(std::chrono::system_clock::now()-s->timestamp).count();return{"external",clamp_probability(s->probability),std::clamp(s->confidence*std::exp(-std::max<long long>(0,age)/48.0),0.0,1.0),true};}
double UniversalModel::expert_weight(const ExpertPrediction& p) const{double b=.25;auto n=expert_brier_n_.find(p.expert);if(n!=expert_brier_n_.end()&&n->second)b=expert_brier_sum_.at(p.expert)/n->second;return p.confidence*std::exp(-cfg_.ensemble_eta*b);}
FairValue UniversalModel::predict(const LiveMarket& m,const std::vector<LiveMarket>& u,const ExternalSignalStore& e){FairValue f;f.components={microstructure(m),factor(m,u),graph(m,u),semantic_relative_value(m,u),external_signal(m,e)};double n=0,d=0;for(const auto&p:f.components)if(p.active&&p.confidence>0){double w=expert_weight(p);n+=w*p.probability;d+=w;}f.probability=clamp_probability(d>0?n/d:m.yes_book.midpoint());double v=0;if(d>0){for(const auto&p:f.components)if(p.active&&p.confidence>0){double w=expert_weight(p),x=p.probability-f.probability;v+=w*x*x;}v/=d;}f.uncertainty=std::clamp(std::sqrt(v)+.15*std::clamp(m.yes_book.spread()/cfg_.max_spread,0.0,1.0),.005,.5);last_prediction_[m.market.id]=f;return f;}
void UniversalModel::update_history(const std::vector<LiveMarket>& u){for(const auto&m:u){auto&h=price_history_[m.market.id];h.push_back(m.yes_book.midpoint());while(h.size()>cfg_.factor_history)h.pop_front();}}
void UniversalModel::observe_resolution(const std::string& id,bool yes){auto i=last_prediction_.find(id);if(i==last_prediction_.end())return;double y=yes?1:0;for(const auto&p:i->second.components)if(p.active){expert_brier_sum_[p.expert]+=(p.probability-y)*(p.probability-y);++expert_brier_n_[p.expert];}}

RiskManager::RiskManager(EngineConfig c):cfg_(c),peak_equity_(c.initial_capital){} double RiskManager::drawdown(double e) const{return peak_equity_>0?std::max(0.0,1-e/peak_equity_):0;}
void RiskManager::update_equity(double e){peak_equity_=std::max(peak_equity_,e);if(drawdown(e)>=cfg_.max_drawdown)killed_=true;}
double RiskManager::allowed_notional(const TradeIdea&i,double e,double gross,double market,double event) const{if(killed_||e<=0||i.net_edge<=0)return 0;double rb=std::max(0.0,cfg_.max_gross_fraction*e-gross),mb=std::max(0.0,cfg_.max_market_fraction*e-market),eb=std::max(0.0,cfg_.max_event_fraction*e-event),tb=cfg_.max_trade_fraction*e,p=std::clamp(i.entry_price,.001,.999),q=std::clamp(i.fair_probability,.001,.999),kb=cfg_.kelly_fraction*std::max(0.0,(q-p)/(1-p))*e;return std::max(0.0,std::min({rb,mb,eb,tb,kb}));}

PaperBroker::PaperBroker(double c):cash_(c){} bool PaperBroker::buy(const TradeIdea&i,double n){if(n<=0||n>cash_||i.entry_price<=0)return false;positions_.push_back({i.market_id,i.token_id,i.outcome,n/i.entry_price,n});cash_-=n;return true;}
double PaperBroker::gross_cost() const{double x=0;for(const auto&p:positions_)x+=p.cost;return x;} double PaperBroker::marked_equity(const std::unordered_map<std::string,double>& mid) const{double e=cash_;for(const auto&p:positions_){auto i=mid.find(p.token_id);e+=p.shares*(i==mid.end()?p.cost/std::max(p.shares,1e-12):i->second);}return e;}
double PaperBroker::market_exposure(const std::string&id) const{double x=0;for(const auto&p:positions_)if(p.market_id==id)x+=p.cost;return x;} double PaperBroker::event_exposure(const std::string&e,const std::unordered_map<std::string,std::string>& map) const{if(e.empty())return 0;double x=0;for(const auto&p:positions_){auto i=map.find(p.market_id);if(i!=map.end()&&i->second==e)x+=p.cost;}return x;}

QuantEngine::QuantEngine(EngineConfig c):cfg_(c),client_(),model_(c),risk_(c),broker_(c.initial_capital),last_equity_(c.initial_capital){std::filesystem::create_directories("runs");} void QuantEngine::set_external_csv(const std::string&p){external_.load_csv(p);}
TradeIdea QuantEngine::make_idea(const LiveMarket&m,const FairValue&f) const{double qy=f.probability,qn=1-qy,ye=m.yes_book.best_ask,ne=m.no_book.best_ask;TradeIdea i;i.market_id=m.market.id;i.question=m.market.question;i.uncertainty=f.uncertainty;if(qy-ye>=qn-ne){i.token_id=m.market.yes_token;i.outcome="YES";i.fair_probability=qy;i.entry_price=ye;i.raw_edge=qy-ye;}else{i.token_id=m.market.no_token;i.outcome="NO";i.fair_probability=qn;i.entry_price=ne;i.raw_edge=qn-ne;}double sp=i.outcome=="YES"?m.yes_book.spread():m.no_book.spread(),fr=m.market.fee_rate>=0?m.market.fee_rate:cfg_.fallback_taker_fee_rate,fee=fr*i.entry_price*(1-i.entry_price);i.estimated_cost=.5*sp+fee+(cfg_.slippage_bps+cfg_.assumed_fee_bps)/10000+cfg_.uncertainty_buffer*f.uncertainty;i.net_edge=i.raw_edge-i.estimated_cost;return i;}
void QuantEngine::append_signal_log(const TradeIdea&i,const FairValue&f) const{bool fresh=!std::filesystem::exists("runs/signals.csv");std::ofstream o("runs/signals.csv",std::ios::app);if(fresh)o<<"time,market_id,outcome,fair,entry,raw_edge,cost,net_edge,uncertainty,question\n";std::string q=i.question;std::replace(q.begin(),q.end(),',',';');o<<now_iso()<<','<<i.market_id<<','<<i.outcome<<','<<f.probability<<','<<i.entry_price<<','<<i.raw_edge<<','<<i.estimated_cost<<','<<i.net_edge<<','<<i.uncertainty<<','<<q<<'\n';}
void QuantEngine::append_trade_log(const TradeIdea&i,double n) const{bool fresh=!std::filesystem::exists("runs/trades.csv");std::ofstream o("runs/trades.csv",std::ios::app);if(fresh)o<<"time,market_id,token_id,outcome,entry,notional,net_edge\n";o<<now_iso()<<','<<i.market_id<<','<<i.token_id<<','<<i.outcome<<','<<i.entry_price<<','<<n<<','<<i.net_edge<<'\n';}
void QuantEngine::run_once(){auto u=client_.snapshot(cfg_.market_limit,cfg_.min_liquidity,cfg_.max_spread);if(u.empty())throw std::runtime_error("no live tradable markets returned after filters");std::unordered_map<std::string,double> mid;std::unordered_map<std::string,std::string> m2e;for(const auto&m:u){mid[m.market.yes_token]=m.yes_book.midpoint();mid[m.market.no_token]=m.no_book.midpoint();m2e[m.market.id]=m.market.event_id;}last_equity_=broker_.marked_equity(mid);risk_.update_equity(last_equity_);last_ideas_.clear();for(const auto&m:u){auto f=model_.predict(m,u,external_);auto i=make_idea(m,f);append_signal_log(i,f);if(i.net_edge<cfg_.min_net_edge||risk_.killed())continue;double n=risk_.allowed_notional(i,last_equity_,broker_.gross_cost(),broker_.market_exposure(m.market.id),broker_.event_exposure(m.market.event_id,m2e));i.desired_notional=n;last_ideas_.push_back(i);if(n>0&&broker_.buy(i,n))append_trade_log(i,n);}model_.update_history(u);last_equity_=broker_.marked_equity(mid);risk_.update_equity(last_equity_);std::sort(last_ideas_.begin(),last_ideas_.end(),[](const auto&a,const auto&b){return a.net_edge>b.net_edge;});std::cout<<"\nPOLYMARKET UNIVERSAL ENGINE — PAPER ONLY\nmarkets="<<u.size()<<" equity="<<std::fixed<<std::setprecision(2)<<last_equity_<<" drawdown="<<100*risk_.drawdown(last_equity_)<<"% killed="<<(risk_.killed()?"YES":"no")<<"\n";std::cout<<std::left<<std::setw(9)<<"market"<<std::setw(5)<<"side"<<std::setw(9)<<"entry"<<std::setw(9)<<"fair"<<std::setw(10)<<"netEdge"<<std::setw(11)<<"notional"<<"question\n";for(std::size_t k=0;k<std::min<std::size_t>(10,last_ideas_.size());++k){const auto&i=last_ideas_[k];std::cout<<std::setw(9)<<i.market_id.substr(0,8)<<std::setw(5)<<i.outcome<<std::setw(9)<<std::setprecision(4)<<i.entry_price<<std::setw(9)<<i.fair_probability<<std::setw(10)<<i.net_edge<<std::setw(11)<<std::setprecision(2)<<i.desired_notional<<i.question.substr(0,75)<<'\n';}}

} // namespace poly
