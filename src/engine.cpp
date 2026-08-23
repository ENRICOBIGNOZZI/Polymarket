#include "pm/engine.hpp"
#include <boost/json.hpp>
#include <chrono>
#include <cctype>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <set>
#include <sstream>
#include <stdexcept>
#include <thread>

namespace pm {
namespace json=boost::json;
namespace fs=std::filesystem;
namespace {

std::int64_t now_s(){
    return std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
}

double jdouble(const json::object& o,const char* k,double d){
    auto it=o.find(k);if(it==o.end())return d;auto const&v=it->value();
    try{if(v.is_double())return v.as_double();if(v.is_int64())return(double)v.as_int64();if(v.is_uint64())return(double)v.as_uint64();if(v.is_string())return std::stod(std::string(v.as_string()));}catch(...){}
    return d;
}
std::size_t jsize(const json::object& o,const char* k,std::size_t d){return static_cast<std::size_t>(std::max(0.0,jdouble(o,k,(double)d)));}
bool jbool(const json::object&o,const char*k,bool d){auto it=o.find(k);if(it==o.end())return d;if(it->value().is_bool())return it->value().as_bool();return d;}
std::string jstring(const json::object&o,const char*k,const std::string&d){auto it=o.find(k);if(it!=o.end()&&it->value().is_string())return std::string(it->value().as_string());return d;}

std::vector<std::string> split_csv_line(const std::string& line){
    std::vector<std::string> out;std::string cur;bool q=false;
    for(std::size_t i=0;i<line.size();++i){char c=line[i];if(c=='"'){if(q&&i+1<line.size()&&line[i+1]=='"'){cur.push_back('"');++i;}else q=!q;}else if(c==','&&!q){out.push_back(cur);cur.clear();}else cur.push_back(c);}out.push_back(cur);return out;
}

std::vector<std::string> words(const std::string&s){
    std::vector<std::string>w;std::string cur;
    for(unsigned char c:s){if(std::isalnum(c)){cur.push_back((char)std::tolower(c));}else if(cur.size()>=3){w.push_back(cur);cur.clear();}else cur.clear();}
    if(cur.size()>=3)w.push_back(cur);std::sort(w.begin(),w.end());w.erase(std::unique(w.begin(),w.end()),w.end());return w;
}

double jaccard(const std::string&a,const std::string&b){
    auto x=words(a),y=words(b);std::size_t i=0,j=0,inter=0,uni=0;
    while(i<x.size()||j<y.size()){
        if(i==x.size()){++uni;++j;}else if(j==y.size()){++uni;++i;}else if(x[i]==y[j]){++inter;++uni;++i;++j;}else if(x[i]<y[j]){++uni;++i;}else{++uni;++j;}
    }
    return uni?double(inter)/double(uni):0.0;
}

double sample_sd(const std::vector<double>& x,double mean){
    if(x.size()<2)return 0.0;double s=0;for(double v:x){double d=v-mean;s+=d*d;}return std::sqrt(s/double(x.size()-1));
}

double dot(const std::vector<double>&a,const std::vector<double>&b){return std::inner_product(a.begin(),a.end(),b.begin(),0.0);}

} // namespace

Engine::Engine(Config cfg):cfg_(std::move(cfg)),api_(cfg_),cash_(cfg_.starting_capital),peak_equity_(cfg_.starting_capital){ensure_runtime();load_state();}

Config Engine::load_config(const std::string& path){
    Config c;std::ifstream f(path);if(!f)throw std::runtime_error("Cannot open config: "+path);std::stringstream ss;ss<<f.rdbuf();auto root=json::parse(ss.str());if(!root.is_object())throw std::runtime_error("Config must be JSON object");auto const&o=root.as_object();
    c.gamma_url=jstring(o,"gamma_url",c.gamma_url);c.clob_url=jstring(o,"clob_url",c.clob_url);c.run_dir=jstring(o,"run_dir",c.run_dir);c.external_signals_file=jstring(o,"external_signals_file",c.external_signals_file);
    c.market_limit=jsize(o,"market_limit",c.market_limit);c.gamma_page_size=jsize(o,"gamma_page_size",c.gamma_page_size);c.books_batch_size=jsize(o,"books_batch_size",c.books_batch_size);c.interval_seconds=(int)jdouble(o,"interval_seconds",c.interval_seconds);
    c.starting_capital=jdouble(o,"starting_capital",c.starting_capital);c.min_liquidity=jdouble(o,"min_liquidity",c.min_liquidity);c.min_volume24h=jdouble(o,"min_volume24h",c.min_volume24h);c.min_mid=jdouble(o,"min_mid",c.min_mid);c.max_mid=jdouble(o,"max_mid",c.max_mid);c.max_spread=jdouble(o,"max_spread",c.max_spread);
    c.min_net_edge=jdouble(o,"min_net_edge",c.min_net_edge);c.uncertainty_penalty=jdouble(o,"uncertainty_penalty",c.uncertainty_penalty);c.slippage_bps=jdouble(o,"slippage_bps",c.slippage_bps);c.fractional_kelly=jdouble(o,"fractional_kelly",c.fractional_kelly);c.max_trade_usd=jdouble(o,"max_trade_usd",c.max_trade_usd);c.max_market_fraction=jdouble(o,"max_market_fraction",c.max_market_fraction);c.max_event_fraction=jdouble(o,"max_event_fraction",c.max_event_fraction);c.max_gross_fraction=jdouble(o,"max_gross_fraction",c.max_gross_fraction);c.max_drawdown=jdouble(o,"max_drawdown",c.max_drawdown);
    c.history_bootstrap=jbool(o,"history_bootstrap",c.history_bootstrap);c.bootstrap_market_limit=jsize(o,"bootstrap_market_limit",c.bootstrap_market_limit);c.history_lookback_hours=jsize(o,"history_lookback_hours",c.history_lookback_hours);c.history_fidelity_minutes=jsize(o,"history_fidelity_minutes",c.history_fidelity_minutes);c.pca_window=jsize(o,"pca_window",c.pca_window);c.pca_min_history=jsize(o,"pca_min_history",c.pca_min_history);c.pca_universe=jsize(o,"pca_universe",c.pca_universe);c.pca_mr_strength=jdouble(o,"pca_mr_strength",c.pca_mr_strength);c.pca_min_residual_z=jdouble(o,"pca_min_residual_z",c.pca_min_residual_z);
    c.graph_max_sum_error=jdouble(o,"graph_max_sum_error",c.graph_max_sum_error);c.semantic_min_similarity=jdouble(o,"semantic_min_similarity",c.semantic_min_similarity);c.semantic_shrink=jdouble(o,"semantic_shrink",c.semantic_shrink);c.scan_only=jbool(o,"scan_only",c.scan_only);
    if(auto it=o.find("expert_weights");it!=o.end()&&it->value().is_object())for(auto const&kv:it->value().as_object())c.expert_weights[std::string(kv.key())]=jdouble(it->value().as_object(),std::string(kv.key()).c_str(),c.expert_weights[std::string(kv.key())]);
    if(c.min_mid>=c.max_mid)throw std::runtime_error("min_mid must be below max_mid");
    return c;
}

void Engine::ensure_runtime(){
    fs::create_directories(cfg_.run_dir);
    auto ensure=[&](const std::string&n,const std::string&h){auto p=fs::path(cfg_.run_dir)/n;if(!fs::exists(p)||fs::file_size(p)==0){std::ofstream f(p);f<<h<<"\n";}};
    ensure("signals.csv","timestamp,market_id,slug,side,mid,exec_price,fair_side,fair_yes,uncertainty,fee_per_share,slippage_per_share,gross_edge,cost_adjusted_edge,net_edge,score,desired_notional,experts");
    ensure("fills.csv","timestamp,market_id,slug,action,side,shares,price,notional,fee");
    ensure("history.csv","timestamp,market_id,mid");
    ensure("broker_state.csv","market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid");
    ensure("risk_state.csv","cash,peak_equity,killed");
    ensure("expert_scores.csv","expert,brier,count");
    ensure("forecast_state.csv","market_id,expert,q_yes");
    ensure("arbitrage.csv","timestamp,type,market_or_event,raw_edge,net_edge,legs");
}

void Engine::load_state(){
    {std::ifstream f(fs::path(cfg_.run_dir)/"risk_state.csv");std::string l;std::getline(f,l);if(std::getline(f,l)){auto x=split_csv_line(l);if(x.size()>=3){try{cash_=std::stod(x[0]);peak_equity_=std::stod(x[1]);killed_=std::stoi(x[2])!=0;}catch(...){}}}}
    {std::ifstream f(fs::path(cfg_.run_dir)/"broker_state.csv");std::string l;std::getline(f,l);while(std::getline(f,l)){auto x=split_csv_line(l);if(x.size()<9)continue;try{Position p{x[0],x[1],x[2],x[3],x[4],std::stod(x[5]),std::stod(x[6]),std::stod(x[7]),std::stod(x[8])};positions_[p.market_id]=std::move(p);}catch(...){}}}
    {std::ifstream f(fs::path(cfg_.run_dir)/"history.csv");std::string l;std::getline(f,l);while(std::getline(f,l)){auto x=split_csv_line(l);if(x.size()<3)continue;try{auto&d=history_[x[1]];d.push_back(std::stod(x[2]));while(d.size()>cfg_.pca_window+2)d.pop_front();}catch(...){}}}
    {std::ifstream f(fs::path(cfg_.run_dir)/"expert_scores.csv");std::string l;std::getline(f,l);while(std::getline(f,l)){auto x=split_csv_line(l);if(x.size()<3)continue;try{expert_brier_[x[0]]=std::stod(x[1]);expert_count_[x[0]]=std::stod(x[2]);}catch(...){}}}
    {std::ifstream f(fs::path(cfg_.run_dir)/"forecast_state.csv");std::string l;std::getline(f,l);while(std::getline(f,l)){auto x=split_csv_line(l);if(x.size()<3)continue;try{last_forecasts_[x[0]][x[1]]=std::stod(x[2]);}catch(...){}}}
}

std::string Engine::csv_escape(const std::string&s){if(s.find_first_of(",\"\n")==std::string::npos)return s;std::string o="\"";for(char c:s){if(c=='\"')o+="\"\"";else o+=c;}return o+"\"";}

void Engine::append_signal(const Signal&s){
    std::ofstream f(fs::path(cfg_.run_dir)/"signals.csv",std::ios::app);std::ostringstream ex;
    for(std::size_t i=0;i<s.experts.size();++i){if(i)ex<<"|";ex<<s.experts[i].name<<":"<<s.experts[i].q_yes<<":"<<s.experts[i].confidence;}
    f<<now_s()<<","<<csv_escape(s.market_id)<<","<<csv_escape(s.slug)<<","<<s.side<<","<<s.market_mid<<","<<s.executable_price<<","<<s.fair_side<<","<<s.fair_yes<<","<<s.uncertainty<<","<<s.fee_per_share<<","<<s.slippage_per_share<<","<<s.gross_edge<<","<<s.cost_adjusted_edge<<","<<s.net_edge<<","<<s.score<<","<<s.desired_notional<<","<<csv_escape(ex.str())<<"\n";
}
void Engine::append_fill(const Fill&x){std::ofstream f(fs::path(cfg_.run_dir)/"fills.csv",std::ios::app);f<<x.ts<<","<<csv_escape(x.market_id)<<","<<csv_escape(x.slug)<<","<<x.action<<","<<x.side<<","<<x.shares<<","<<x.price<<","<<x.notional<<","<<x.fee<<"\n";}
void Engine::append_history(std::int64_t ts,const Market&m,double mid){std::ofstream f(fs::path(cfg_.run_dir)/"history.csv",std::ios::app);f<<ts<<","<<csv_escape(m.id)<<","<<mid<<"\n";auto&d=history_[m.id];d.push_back(mid);while(d.size()>cfg_.pca_window+2)d.pop_front();}

void Engine::bootstrap_history(const std::vector<Market>&markets){
    if(!cfg_.history_bootstrap||cfg_.bootstrap_market_limit==0)return;
    std::vector<const Market*> need;
    for(auto const&m:markets){auto it=history_.find(m.id);if(it==history_.end()||it->second.empty())need.push_back(&m);}
    std::sort(need.begin(),need.end(),[](auto*a,auto*b){return a->liquidity>b->liquidity;});
    if(need.size()>cfg_.bootstrap_market_limit)need.resize(cfg_.bootstrap_market_limit);
    if(need.empty())return;
    std::vector<std::string>tokens;tokens.reserve(need.size());for(auto*m:need)tokens.push_back(m->yes_token);
    const auto end=now_s();const auto start=end-static_cast<std::int64_t>(cfg_.history_lookback_hours)*3600;
    try{
        auto data=api_.fetch_price_history(tokens,start,end,cfg_.history_fidelity_minutes);
        std::ofstream hf(fs::path(cfg_.run_dir)/"history.csv",std::ios::app);
        std::size_t booted=0,points=0;
        for(auto*m:need){auto it=data.find(m->yes_token);if(it==data.end()||it->second.size()<2)continue;auto&d=history_[m->id];if(!d.empty())continue;const auto&v=it->second;const std::size_t keep=std::min<std::size_t>(v.size(),cfg_.pca_window+1);for(std::size_t i=v.size()-keep;i<v.size();++i){if(v[i].price<=0||v[i].price>=1)continue;d.push_back(v[i].price);hf<<v[i].ts<<","<<csv_escape(m->id)<<","<<v[i].price<<"\n";++points;}while(d.size()>cfg_.pca_window+2)d.pop_front();if(d.size()>=2)++booted;}
        if(booted)std::cout<<"history_bootstrap markets="<<booted<<" points="<<points<<"\n";
    }catch(std::exception const&e){std::cerr<<"history bootstrap failed: "<<e.what()<<"\n";}
}

FeeDetails Engine::fee_for(const Market&m){auto it=fee_cache_.find(m.condition_id);if(it!=fee_cache_.end())return it->second;auto fd=api_.fetch_fee_details(m);fee_cache_[m.condition_id]=fd;return fd;}

void Engine::persist_state(double eq,double gross){
    peak_equity_=std::max(peak_equity_,eq);if(peak_equity_>0&&1.0-eq/peak_equity_>=cfg_.max_drawdown)killed_=true;
    {std::ofstream f(fs::path(cfg_.run_dir)/"broker_state.csv");f<<"market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid\n";for(auto const&[id,p]:positions_)f<<csv_escape(p.market_id)<<","<<csv_escape(p.event_id)<<","<<csv_escape(p.slug)<<","<<p.side<<","<<p.token_id<<","<<p.shares<<","<<p.avg_price<<","<<p.cost_basis<<","<<p.fees_paid<<"\n";}
    {std::ofstream f(fs::path(cfg_.run_dir)/"risk_state.csv");f<<"cash,peak_equity,killed\n"<<cash_<<","<<peak_equity_<<","<<(killed_?1:0)<<"\n";}
    {std::ofstream f(fs::path(cfg_.run_dir)/"expert_scores.csv");f<<"expert,brier,count\n";std::set<std::string>names;for(auto const&kv:cfg_.expert_weights)names.insert(kv.first);for(auto const&n:names)f<<n<<","<<(expert_brier_.count(n)?expert_brier_.at(n):0.25)<<","<<(expert_count_.count(n)?expert_count_.at(n):0.0)<<"\n";}
    {std::ofstream f(fs::path(cfg_.run_dir)/"forecast_state.csv");f<<"market_id,expert,q_yes\n";for(auto const&[mid,mp]:last_forecasts_)for(auto const&[name,q]:mp)f<<csv_escape(mid)<<","<<csv_escape(name)<<","<<q<<"\n";}
    json::object st{{"timestamp",now_s()},{"cash",cash_},{"equity",eq},{"peak_equity",peak_equity_},{"drawdown",peak_equity_>0?1.0-eq/peak_equity_:0.0},{"gross_exposure",gross},{"open_positions",positions_.size()},{"killed",killed_},{"mode","paper-v2"}};std::ofstream f(fs::path(cfg_.run_dir)/"status.json");f<<json::serialize(st)<<"\n";
}

std::unordered_map<std::string,ExternalSignal> Engine::load_external() const{
    std::unordered_map<std::string,ExternalSignal>out;std::ifstream f(cfg_.external_signals_file);if(!f)return out;std::string l;std::getline(f,l);const auto now=now_s();
    while(std::getline(f,l)){auto x=split_csv_line(l);if(x.size()<5)continue;try{ExternalSignal s{std::stod(x[1]),std::stod(x[2]),x[3],std::stoll(x[4])};double age=std::max<std::int64_t>(0,now-s.timestamp);s.confidence*=std::exp(-std::log(2.0)*double(age)/(6.0*3600.0));if(s.confidence>1e-4)out[x[0]]=s;}catch(...){}}
    return out;
}

std::optional<std::pair<double,double>> Engine::walk_book(const Book&book,bool buy,double requested_notional){
    if(requested_notional<=0)return std::nullopt;auto lv=buy?book.asks:book.bids;if(buy)std::sort(lv.begin(),lv.end(),[](auto const&a,auto const&b){return a.price<b.price;});else std::sort(lv.begin(),lv.end(),[](auto const&a,auto const&b){return a.price>b.price;});double rem=requested_notional,sh=0,cash=0;
    for(auto const&x:lv){if(x.price<=0||x.size<=0)continue;double cap=x.price*x.size;double take=std::min(rem,cap);double q=take/x.price;sh+=q;cash+=q*x.price;rem-=take;if(rem<=1e-9)break;}
    if(sh<=0||rem>std::max(0.01,requested_notional*0.02))return std::nullopt;return std::pair{sh,cash/sh};
}
double Engine::protocol_fee(double shares,double price,const FeeDetails&fd){if(shares<=0||price<=0||price>=1||fd.rate<=0)return 0.0;return shares*fd.rate*std::pow(price*(1.0-price),std::max(0.0,fd.exponent));}
double Engine::full_kelly(double q,double p){if(p<=0||p>=1)return 0;return std::max(0.0,(q-p)/(1.0-p));}

std::unordered_map<std::string,Engine::Adjustment> Engine::pca_adjustments(const std::vector<Market>&markets,const std::unordered_map<std::string,Book>&yes_books) const{
    struct S{const Market*m=nullptr;double mean=0,sd=0;std::vector<double>r;};
    std::vector<const Market*>base;
    for(auto const&m:markets){
        auto h=history_.find(m.id);
        auto b=yes_books.find(m.yes_token);
        if(h==history_.end()||b==yes_books.end())continue;
        double cur=b->second.midpoint();
        if(!std::isfinite(cur)||cur<=cfg_.min_mid||cur>=cfg_.max_mid||b->second.spread()>cfg_.max_spread)continue;
        if(h->second.size()<cfg_.pca_min_history)continue;
        base.push_back(&m);
    }
    std::sort(base.begin(),base.end(),[](auto*a,auto*b){return a->liquidity>b->liquidity;});if(base.size()>cfg_.pca_universe)base.resize(cfg_.pca_universe);if(base.size()<3)return{};
    std::size_t T=cfg_.pca_window;for(auto*m:base)T=std::min(T,history_.at(m->id).size()-1);if(T+1<cfg_.pca_min_history||T<8)return{};

    std::vector<S> s;s.reserve(base.size());
    for(auto*m:base){S z;z.m=m;auto const&h=history_.at(m->id);const std::size_t start=h.size()-(T+1);z.r.reserve(T);for(std::size_t t=0;t<T;++t){double dr=logit(h[start+t+1])-logit(h[start+t]);z.r.push_back(std::clamp(dr,-2.0,2.0));}z.mean=std::accumulate(z.r.begin(),z.r.end(),0.0)/double(T);z.sd=sample_sd(z.r,z.mean);if(z.sd>1e-4)s.push_back(std::move(z));}
    const std::size_t N=s.size();if(N<3)return{};

    std::vector<std::vector<double>>Y(T,std::vector<double>(N));
    for(std::size_t j=0;j<N;++j)for(std::size_t t=0;t<T;++t)Y[t][j]=std::clamp((s[j].r[t]-s[j].mean)/s[j].sd,-4.0,4.0);
    std::vector<std::vector<double>>C(N,std::vector<double>(N));
    for(std::size_t i=0;i<N;++i)for(std::size_t j=i;j<N;++j){double q=0;for(std::size_t t=0;t<T;++t)q+=Y[t][i]*Y[t][j];q/=double(T);C[i][j]=C[j][i]=q;}
    std::vector<double>v(N,1.0/std::sqrt(double(N)));
    for(int it=0;it<50;++it){std::vector<double>w(N);for(std::size_t i=0;i<N;++i)for(std::size_t j=0;j<N;++j)w[i]+=C[i][j]*v[j];double n=std::sqrt(dot(w,w));if(n<1e-12)return{};for(std::size_t i=0;i<N;++i)v[i]=w[i]/n;}
    std::vector<double>Cv(N);for(std::size_t i=0;i<N;++i)for(std::size_t j=0;j<N;++j)Cv[i]+=C[i][j]*v[j];const double lambda1=dot(v,Cv);double trace=0;for(std::size_t i=0;i<N;++i)trace+=C[i][i];const double explained=trace>1e-12?std::clamp(lambda1/trace,0.0,1.0):0.0;

    std::vector<std::vector<double>>resid(T,std::vector<double>(N));
    for(std::size_t t=0;t<T;++t){double ft=dot(v,Y[t]);for(std::size_t j=0;j<N;++j)resid[t][j]=Y[t][j]-v[j]*ft;}
    std::vector<double>resid_sd(N,1.0),phi(N,0.0);
    for(std::size_t j=0;j<N;++j){double m=0;for(std::size_t t=0;t<T;++t)m+=resid[t][j];m/=double(T);std::vector<double>tmp(T);for(std::size_t t=0;t<T;++t)tmp[t]=resid[t][j];resid_sd[j]=std::max(0.20,sample_sd(tmp,m));double num=0,den=0;for(std::size_t t=1;t<T;++t){num+=resid[t][j]*resid[t-1][j];den+=resid[t-1][j]*resid[t-1][j];}phi[j]=den>1e-8?std::clamp(num/den,-0.75,0.75):0.0;}

    std::vector<double>x(N);for(std::size_t j=0;j<N;++j){auto const&h=history_.at(s[j].m->id);double cur=yes_books.at(s[j].m->yes_token).midpoint();double dr=logit(cur)-logit(h.back());x[j]=std::clamp((dr-s[j].mean)/s[j].sd,-6.0,6.0);}double F=dot(v,x);
    std::unordered_map<std::string,Adjustment>out;
    for(std::size_t j=0;j<N;++j){double e=x[j]-v[j]*F;double rz=e/resid_sd[j];if(std::abs(rz)<cfg_.pca_min_residual_z)continue;double cur=yes_books.at(s[j].m->yes_token).midpoint();double persistence=std::clamp(1.0-phi[j],0.25,1.50);double delta=std::clamp(cfg_.pca_mr_strength*persistence*e*s[j].sd,-0.75,0.75);double q=logistic(logit(cur)-delta);double zc=std::clamp((std::abs(rz)-cfg_.pca_min_residual_z)/2.5+0.20,0.10,1.0);double sc=std::min(1.0,double(T)/48.0);double fc=std::clamp(0.35+1.5*explained,0.35,1.0);out[s[j].m->id]={q,std::clamp(zc*sc*fc,0.05,1.0)};}
    return out;
}

std::unordered_map<std::string,Engine::Adjustment> Engine::graph_adjustments(const std::vector<Market>&markets,const std::unordered_map<std::string,Book>&yes_books) const{
    std::unordered_map<std::string,std::vector<const Market*>>groups;for(auto const&m:markets)if(m.neg_risk)groups[m.event_id].push_back(&m);std::unordered_map<std::string,Adjustment>out;
    for(auto const&[event,v]:groups){if(v.size()<2)continue;double sum=0,varsum=0;bool ok=true;struct G{const Market*m;double p,var;};std::vector<G>g;g.reserve(v.size());
        for(auto*m:v){auto it=yes_books.find(m->yes_token);if(it==yes_books.end()){ok=false;break;}double p=it->second.midpoint(),sp=it->second.spread(),dep=it->second.weighted_depth(true)+it->second.weighted_depth(false);if(!std::isfinite(p)||!std::isfinite(sp)){ok=false;break;}double var=std::max(1e-6,sp*sp+1.0/(dep+1.0));g.push_back({m,p,var});sum+=p;varsum+=var;}
        if(!ok||varsum<=0||std::abs(sum-1.0)>cfg_.graph_max_sum_error)continue;double conf=std::clamp(1.0-std::abs(sum-1.0)/std::max(1e-6,cfg_.graph_max_sum_error),0.10,1.0);
        std::vector<double>q(g.size());double qsum=0;for(std::size_t i=0;i<g.size();++i){q[i]=std::clamp(g[i].p+(1.0-sum)*g[i].var/varsum,0.001,0.999);qsum+=q[i];}if(qsum<=0)continue;for(std::size_t i=0;i<g.size();++i)out[g[i].m->id]={std::clamp(q[i]/qsum,0.001,0.999),0.85*conf};
    }
    return out;
}

std::vector<ExpertPrediction> Engine::build_experts(const Market&m,const Book&yes,const Book&no,const std::vector<Market>&universe,const std::unordered_map<std::string,Book>&yes_books,const std::unordered_map<std::string,ExternalSignal>&external,const std::unordered_map<std::string,Adjustment>&pca,const std::unordered_map<std::string,Adjustment>&graph) const{
    std::vector<ExpertPrediction>r;double mid=yes.midpoint();
    if(std::isfinite(mid)){
        double y=yes.microprice(),n=no.microprice();double dy=yes.weighted_depth(true)+yes.weighted_depth(false),dn=no.weighted_depth(true)+no.weighted_depth(false);double wy=std::sqrt(std::max(0.0,dy))/(1.0+20.0*yes.spread()),wn=std::sqrt(std::max(0.0,dn))/(1.0+20.0*no.spread());double q=mid;
        if(std::isfinite(y)&&std::isfinite(n)&&wy+wn>1e-12)q=(wy*y+wn*(1.0-n))/(wy+wn);else if(std::isfinite(y))q=y;
        double parity=(std::isfinite(y)&&std::isfinite(n))?std::abs(y-(1.0-n)):0.25;double liq=(dy+dn)/(dy+dn+200.0);double spc=std::exp(-5.0*(yes.spread()+no.spread()));double conf=std::clamp(liq*spc*std::exp(-8.0*parity),0.02,1.0);r.push_back({"micro",std::clamp(q,0.001,0.999),conf});
    }
    if(auto it=pca.find(m.id);it!=pca.end())r.push_back({"pca",it->second.first,it->second.second});
    if(auto it=graph.find(m.id);it!=graph.end())r.push_back({"graph",it->second.first,it->second.second});
    if(std::isfinite(mid)){
        double sw=0,spv=0;for(auto const&o:universe){if(o.id==m.id)continue;double sim=jaccard(m.question,o.question);if(sim<cfg_.semantic_min_similarity)continue;auto it=yes_books.find(o.yes_token);if(it==yes_books.end())continue;double p=it->second.midpoint();if(!std::isfinite(p))continue;double w=sim*sim*std::sqrt(std::max(1.0,o.liquidity));sw+=w;spv+=w*p;}
        if(sw>0){double peer=spv/sw;double q=(1.0-cfg_.semantic_shrink)*mid+cfg_.semantic_shrink*peer;r.push_back({"semantic",std::clamp(q,0.001,0.999),std::min(0.25,0.05+cfg_.semantic_shrink)});}
    }
    auto add_ext=[&](const std::string&k){auto it=external.find(k);if(it!=external.end()){r.push_back({"external",std::clamp(it->second.q_yes,0.001,0.999),std::clamp(it->second.confidence,0.0,1.0)});return true;}return false;};if(!add_ext(m.id)&&!add_ext(m.condition_id))add_ext(m.slug);return r;
}

std::pair<double,double> Engine::ensemble(const std::vector<ExpertPrediction>&p,double spread) const{
    double sw=0,q=0;for(auto const&e:p){double base=cfg_.expert_weights.count(e.name)?cfg_.expert_weights.at(e.name):0.0;double n=expert_count_.count(e.name)?expert_count_.at(e.name):0.0;double b=(n>=5&&expert_brier_.count(e.name))?expert_brier_.at(e.name):0.25;double w=base*std::exp(-2.0*b)*e.confidence;sw+=w;q+=w*e.q_yes;}if(sw<=1e-12)return{0.5,1.0};q/=sw;double v=0;for(auto const&e:p){double base=cfg_.expert_weights.count(e.name)?cfg_.expert_weights.at(e.name):0.0;double n=expert_count_.count(e.name)?expert_count_.at(e.name):0.0;double b=(n>=5&&expert_brier_.count(e.name))?expert_brier_.at(e.name):0.25;double w=base*std::exp(-2.0*b)*e.confidence;v+=w*(e.q_yes-q)*(e.q_yes-q);}v/=sw;double u=std::sqrt(std::max(0.0,v)+0.25*spread*spread);return{std::clamp(q,0.001,0.999),std::clamp(u,1e-4,1.0)};
}

double Engine::equity(const std::unordered_map<std::string,Book>&books) const{double e=cash_;for(auto const&[id,p]:positions_){auto it=books.find(p.token_id);double px=(it!=books.end()&&std::isfinite(it->second.best_bid()))?it->second.best_bid():p.avg_price;e+=p.shares*std::max(0.0,px);}return e;}
double Engine::gross_exposure(const std::unordered_map<std::string,Book>&books) const{double g=0;for(auto const&[id,p]:positions_){auto it=books.find(p.token_id);double px=(it!=books.end()&&std::isfinite(it->second.midpoint()))?it->second.midpoint():p.avg_price;g+=p.shares*std::max(0.0,px);}return g;}
double Engine::open_loss_upper_bound() const{double x=0;for(auto const&[id,p]:positions_)x+=std::max(0.0,p.cost_basis);return x;}
double Engine::market_exposure(const std::string&id) const{auto it=positions_.find(id);return it==positions_.end()?0.0:it->second.cost_basis;}
double Engine::event_exposure(const std::string&e) const{double x=0;for(auto const&[id,p]:positions_)if(p.event_id==e)x+=p.cost_basis;return x;}

double Engine::size_trade(const Signal&s,const Market&m,double eq,double gross) const{
    if(killed_||eq<=0||s.net_edge<=cfg_.min_net_edge)return 0;double k=cfg_.fractional_kelly*full_kelly(s.fair_side,s.executable_price)*eq;double lim=std::min({k,cfg_.max_trade_usd,cfg_.max_market_fraction*eq-market_exposure(m.id),cfg_.max_event_fraction*eq-event_exposure(m.event_id),cfg_.max_gross_fraction*eq-gross,cash_});double ddroom=cfg_.max_drawdown*peak_equity_-(peak_equity_-eq)-gross;lim=std::min(lim,std::max(0.0,ddroom));return std::max(0.0,lim);
}

void Engine::paper_trade(const Signal&s,const Market&m,const Book&book,const FeeDetails&fd,double notional){
    if(positions_.count(m.id)||notional<=0)return;auto w=walk_book(book,true,notional);if(!w)return;double shares=w->first,px=w->second*(1.0+cfg_.slippage_bps/10000.0);double fee=protocol_fee(shares,px,fd);double cost=shares*px+fee;if(cost>cash_||shares<book.min_order_size)return;double realized=s.fair_side-px-fee/shares;if(realized<=cfg_.min_net_edge)return;Position p{m.id,m.event_id,m.slug,s.side,s.side=="YES"?m.yes_token:m.no_token,shares,px,cost,fee};positions_[m.id]=p;cash_-=cost;append_fill({now_s(),m.id,m.slug,"BUY",s.side,shares,px,shares*px,fee});
}

void Engine::maybe_exit(const Market&m,const Book&book,double fair_yes,const FeeDetails&fd){
    auto it=positions_.find(m.id);if(it==positions_.end())return;auto&p=it->second;double fair=p.side=="YES"?fair_yes:1.0-fair_yes;double bid=book.best_bid();if(!std::isfinite(bid))return;if(!killed_&&fair>=bid-cfg_.min_net_edge*0.5)return;auto lv=book.bids;std::sort(lv.begin(),lv.end(),[](auto const&a,auto const&b){return a.price>b.price;});double rem=p.shares,proceeds=0,sold=0;for(auto const&x:lv){double q=std::min(rem,x.size);sold+=q;proceeds+=q*x.price;rem-=q;if(rem<=1e-9)break;}if(sold+1e-9<p.shares)return;double px=(proceeds/sold)*(1.0-cfg_.slippage_bps/10000.0);double fee=protocol_fee(sold,px,fd);cash_+=sold*px-fee;append_fill({now_s(),m.id,m.slug,"SELL",p.side,sold,px,sold*px,fee});positions_.erase(it);
}

void Engine::score_resolved(const std::vector<Market>&markets){
    for(auto const&m:markets){if(!m.resolved_yes)continue;auto fit=last_forecasts_.find(m.id);if(fit!=last_forecasts_.end()){for(auto const&[name,q]:fit->second){double loss=(q-*m.resolved_yes)*(q-*m.resolved_yes);double n=expert_count_[name];expert_brier_[name]=(n<=0)?loss:0.98*expert_brier_[name]+0.02*loss;expert_count_[name]=n+1;}}auto pit=positions_.find(m.id);if(pit!=positions_.end()){double win=(pit->second.side=="YES"?*m.resolved_yes:1-*m.resolved_yes);cash_+=pit->second.shares*win;append_fill({now_s(),m.id,m.slug,"SETTLE",pit->second.side,pit->second.shares,win,pit->second.shares*win,0.0});positions_.erase(pit);}last_forecasts_.erase(m.id);}
}

void Engine::run_once(bool paper,bool scan_only){
    auto markets=api_.discover_markets(cfg_.market_limit,cfg_.min_liquidity);bootstrap_history(markets);
    std::vector<Market>resolution_markets=markets;std::unordered_set<std::string>active_ids;for(auto const&m:markets)active_ids.insert(m.id);for(auto const&[id,p]:positions_)if(!active_ids.count(id)){try{if(auto closed=api_.fetch_market_by_id(id))resolution_markets.push_back(std::move(*closed));}catch(std::exception const&e){std::cerr<<"resolution lookup failed for "<<id<<": "<<e.what()<<"\n";}}score_resolved(resolution_markets);

    std::vector<std::string>tokens;tokens.reserve(markets.size()*2+positions_.size());for(auto const&m:markets){tokens.push_back(m.yes_token);tokens.push_back(m.no_token);}for(auto const&[id,p]:positions_)if(std::find(tokens.begin(),tokens.end(),p.token_id)==tokens.end())tokens.push_back(p.token_id);auto books=tokens.empty()?std::unordered_map<std::string,Book>{}:api_.fetch_books(tokens);

    std::vector<Market>tradable;tradable.reserve(markets.size());std::unordered_map<std::string,Book>yes_books;
    for(auto const&m:markets){if(m.volume24h+1e-12<cfg_.min_volume24h)continue;auto yi=books.find(m.yes_token),ni=books.find(m.no_token);if(yi==books.end()||ni==books.end())continue;double mid=yi->second.midpoint();if(!std::isfinite(mid)||mid<=cfg_.min_mid||mid>=cfg_.max_mid)continue;if(yi->second.spread()>cfg_.max_spread||ni->second.spread()>cfg_.max_spread)continue;if(!std::isfinite(yi->second.best_ask())||!std::isfinite(ni->second.best_ask()))continue;tradable.push_back(m);yes_books[m.yes_token]=yi->second;}

    double eq=equity(books),gross=gross_exposure(books);peak_equity_=std::max(peak_equity_,eq);if(peak_equity_>0&&1.0-eq/peak_equity_>=cfg_.max_drawdown)killed_=true;auto external=load_external();auto pca=pca_adjustments(tradable,yes_books);auto graph=graph_adjustments(tradable,yes_books);
    struct Candidate{Signal s;const Market*m;const Book*b;FeeDetails fd;};std::vector<Candidate>cands;const auto ts=now_s();std::size_t gross_pos=0,cost_pos=0,net_pos=0;double best_net=-1e9;
    std::ofstream arb(fs::path(cfg_.run_dir)/"arbitrage.csv",std::ios::app);

    for(auto const&m:tradable){auto yi=books.find(m.yes_token),ni=books.find(m.no_token);if(yi==books.end()||ni==books.end())continue;double mid=yi->second.midpoint();auto preds=build_experts(m,yi->second,ni->second,tradable,yes_books,external,pca,graph);auto[fair,u]=ensemble(preds,std::max(yi->second.spread(),ni->second.spread()));last_forecasts_[m.id].clear();for(auto const&e:preds)last_forecasts_[m.id][e.name]=e.q_yes;FeeDetails fd=fee_for(m);

        double ay=yi->second.best_ask(),an=ni->second.best_ask();if(std::isfinite(ay)&&std::isfinite(an)){double fy=fd.rate*std::pow(ay*(1.0-ay),std::max(0.0,fd.exponent)),fn=fd.rate*std::pow(an*(1.0-an),std::max(0.0,fd.exponent));double raw=1.0-ay-an;double net=raw-fy-fn-(ay+an)*cfg_.slippage_bps/10000.0;if(net>-0.01)arb<<ts<<",PAIR,"<<csv_escape(m.id)<<","<<raw<<","<<net<<","<<csv_escape(m.yes_token+"|"+m.no_token)<<"\n";}

        auto make=[&](std::string side,const Book&b,double qside){double ask=b.best_ask();if(!std::isfinite(ask))return;Signal s;s.market_id=m.id;s.slug=m.slug;s.side=side;s.market_mid=mid;s.executable_price=ask;s.fair_side=qside;s.fair_yes=fair;s.uncertainty=u;s.fee_per_share=fd.rate*std::pow(ask*(1.0-ask),std::max(0.0,fd.exponent));s.slippage_per_share=ask*cfg_.slippage_bps/10000.0;s.gross_edge=qside-ask;s.cost_adjusted_edge=s.gross_edge-s.fee_per_share-s.slippage_per_share;s.net_edge=s.cost_adjusted_edge-cfg_.uncertainty_penalty*u;s.score=s.net_edge/std::max(1e-4,u);s.experts=preds;if(s.gross_edge>0)++gross_pos;if(s.cost_adjusted_edge>0)++cost_pos;if(s.net_edge>0)++net_pos;best_net=std::max(best_net,s.net_edge);cands.push_back({s,&m,&b,fd});};
        make("YES",yi->second,fair);make("NO",ni->second,1.0-fair);
        auto pit=positions_.find(m.id);if(pit!=positions_.end()){auto bit=books.find(pit->second.token_id);if(bit!=books.end())maybe_exit(m,bit->second,fair,fd);}append_history(ts,m,mid);
    }

    std::unordered_map<std::string,std::vector<const Market*>>ng;for(auto const&m:tradable)if(m.neg_risk)ng[m.event_id].push_back(&m);for(auto const&[event,v]:ng){if(v.size()<2)continue;double raw=1.0,net=1.0;bool ok=true;std::ostringstream legs;for(std::size_t i=0;i<v.size();++i){auto it=books.find(v[i]->yes_token);if(it==books.end()||!std::isfinite(it->second.best_ask())){ok=false;break;}double a=it->second.best_ask();auto fd=fee_for(*v[i]);double f=fd.rate*std::pow(a*(1.0-a),std::max(0.0,fd.exponent));raw-=a;net-=a+f+a*cfg_.slippage_bps/10000.0;if(i)legs<<"|";legs<<v[i]->yes_token;}if(ok&&net>-0.02)arb<<ts<<",NEGRISK_BASKET,"<<csv_escape(event)<<","<<raw<<","<<net<<","<<csv_escape(legs.str())<<"\n";}

    std::sort(cands.begin(),cands.end(),[](auto const&a,auto const&b){return a.s.score>b.s.score;});eq=equity(books);gross=gross_exposure(books);for(auto&c:cands){c.s.desired_notional=size_trade(c.s,*c.m,eq,gross);append_signal(c.s);if(paper&&!scan_only&&!cfg_.scan_only&&c.s.net_edge>cfg_.min_net_edge&&c.s.desired_notional>0&&!positions_.count(c.m->id)){paper_trade(c.s,*c.m,*c.b,c.fd,c.s.desired_notional);eq=equity(books);gross=gross_exposure(books);}}
    persist_state(equity(books),gross_exposure(books));
    std::cout<<"discovered="<<markets.size()<<" tradable="<<tradable.size()<<" candidates="<<cands.size()<<" gross_pos="<<gross_pos<<" cost_pos="<<cost_pos<<" net_pos="<<net_pos<<" best_net="<<(std::isfinite(best_net)?best_net:0.0)<<" pca="<<pca.size()<<" graph="<<graph.size()<<" positions="<<positions_.size()<<" cash="<<cash_<<" equity="<<equity(books)<<" killed="<<killed_<<"\n";
}

void Engine::run_loop(bool paper){for(;;){try{run_once(paper,false);}catch(std::exception const&e){std::cerr<<"tick error: "<<e.what()<<"\n";}std::this_thread::sleep_for(std::chrono::seconds(std::max(1,cfg_.interval_seconds)));}}

} // namespace pm
