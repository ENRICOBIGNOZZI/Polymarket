#include "pm/v7_coinbase_l2.hpp"
#include "pm/v7_external_ws.hpp"
#include <boost/json.hpp>
#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <deque>
#include <iostream>
#include <map>
#include <string>
#include <thread>
#include <vector>
#if !defined(__APPLE__)
#include <stop_token>
#endif

namespace json=boost::json;
using namespace pm::v7::external_fair;
using namespace std::chrono_literals;
namespace {
struct State{std::int64_t receive_ns=0;std::string fingerprint;};
struct Change{std::int64_t receive_ns=0;std::string key;};
std::int64_t now_ns(){return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
int bounded(std::string_view t,int lo,int hi){int v=0;auto r=std::from_chars(t.data(),t.data()+t.size(),v);if(r.ec!=std::errc{}||r.ptr!=t.data()+t.size()||v<lo||v>hi)throw std::invalid_argument("bounded integer argument required");return v;}
const json::value* field(const json::object&o,std::string_view k){auto it=o.find(k);return it==o.end()?nullptr:&it->value();}
bool number(const json::value*v,double&out){if(!v)return false;try{if(v->is_string())out=std::stod(std::string(v->as_string()));else if(v->is_double())out=v->as_double();else if(v->is_int64())out=v->as_int64();else if(v->is_uint64())out=v->as_uint64();else return false;return std::isfinite(out);}catch(...){return false;}}
bool text(const json::value*v,std::string_view x){return v&&v->is_string()&&v->as_string()==x;}
std::string fp(const CoinbaseL2Metrics&m){auto q=[](double v){return static_cast<long long>(std::llround(v*100000000.0));};return std::to_string(q(m.best_bid))+":"+std::to_string(q(m.bid_depth_l1))+":"+std::to_string(q(m.best_ask))+":"+std::to_string(q(m.ask_depth_l1));}
json::object dist(std::vector<double>v){if(v.empty())return{{"count",0},{"p50_us",nullptr},{"p95_us",nullptr},{"p99_us",nullptr},{"min_us",nullptr},{"max_us",nullptr}};std::sort(v.begin(),v.end());auto q=[&](double p){return v[static_cast<std::size_t>(std::ceil(p*v.size()))-1];};return{{"count",v.size()},{"p50_us",q(.5)},{"p95_us",q(.95)},{"p99_us",q(.99)},{"min_us",v.front()},{"max_us",v.back()}};}
class Advanced final:public ExternalFrameObserver{
public:
 explicit Advanced(std::atomic<std::int64_t>&b):begin(b){}
 std::vector<State>states;std::vector<Change>changes_seen;std::atomic<bool>live{false};std::uint64_t frames=0,invalid=0,updates=0,snapshots=0,gaps=0;std::string last_error;
 void on_connection_epoch(std::uint64_t e)noexcept override{epoch=e;book.begin_recovery();last.clear();last_sequence=-1;}
 void on_frame(std::uint64_t e,std::int64_t receive,std::int64_t,std::string_view payload)noexcept override{++frames;try{boost::system::error_code ec;auto raw=json::parse(payload,ec);if(ec||!raw.is_object()){++invalid;return;}auto&o=raw.as_object();if(e!=epoch){epoch=e;book.begin_recovery();last.clear();last_sequence=-1;}if(auto*err=field(o,"error");err){++invalid;last_error=json::serialize(*err);return;}auto*seqv=field(o,"sequence_num");if(!seqv||( !seqv->is_int64()&&!seqv->is_uint64())){++invalid;return;}std::int64_t seq=seqv->is_int64()?seqv->as_int64():static_cast<std::int64_t>(seqv->as_uint64());if(last_sequence>=0&&seq!=last_sequence+1){++gaps;book.begin_recovery();last.clear();}last_sequence=seq;auto*channel=field(o,"channel");if(!text(channel,"l2_data"))return;auto*events=field(o,"events");if(!events||!events->is_array()){++invalid;return;}for(auto&evv:events->as_array()){if(!evv.is_object()){++invalid;continue;}auto&ev=evv.as_object();auto*product=field(ev,"product_id");if(!text(product,"BTC-USD"))continue;auto*type=field(ev,"type");auto*raw_updates=field(ev,"updates");if(!raw_updates||!raw_updates->is_array()||raw_updates->as_array().empty()||raw_updates->as_array().size()>250000){++invalid;continue;}if(text(type,"snapshot")){CoinbaseDepthSnapshot snap;snap.local_receive_monotonic_ns=receive;for(auto&uv:raw_updates->as_array()){CoinbaseDepthChange c;if(!change(uv,c)||c.quantity<=0){++invalid;return;}CoinbaseDepthLevel l{c.price,c.quantity};(c.bid?snap.bids:snap.asks).push_back(l);}if(!book.install_snapshot(snap)){++invalid;return;}++snapshots;live.store(true,std::memory_order_release);record(receive);}else if(text(type,"update")){CoinbaseDepthUpdate up;up.local_receive_monotonic_ns=receive;for(auto&uv:raw_updates->as_array()){CoinbaseDepthChange c;if(!change(uv,c)){++invalid;return;}up.changes.push_back(c);if(receive>=begin.load(std::memory_order_acquire))changes_seen.push_back({receive,change_key(c)});}if(!book.apply_update(up)){++invalid;return;}++updates;live.store(true,std::memory_order_release);record(receive);}}
 }catch(...){++invalid;}}
private:
 bool change(const json::value&v,CoinbaseDepthChange&c){if(!v.is_object())return false;auto&o=v.as_object();auto*side=field(o,"side");if(text(side,"bid"))c.bid=true;else if(text(side,"offer"))c.bid=false;else return false;return number(field(o,"price_level"),c.price)&&number(field(o,"new_quantity"),c.quantity)&&c.price>0&&c.quantity>=0;}
 static std::string change_key(const CoinbaseDepthChange&c){auto q=[](double v){return static_cast<long long>(std::llround(v*100000000.0));};return std::string(c.bid?"B:":"A:")+std::to_string(q(c.price))+":"+std::to_string(q(c.quantity));}
 void record(std::int64_t r){auto m=book.metrics();if(!m.valid)return;auto f=fp(m);if(f==last)return;last=f;if(r>=begin.load(std::memory_order_acquire))states.push_back({r,std::move(f)});}
 std::atomic<std::int64_t>&begin;CoinbaseL2Book book;std::uint64_t epoch=0;std::int64_t last_sequence=-1;std::string last;
};
class ExchangeBatch final:public ExternalFrameObserver{
public:
 explicit ExchangeBatch(std::atomic<std::int64_t>&b):begin(b){}
 std::vector<State>states;std::vector<Change>changes_seen;std::atomic<bool>live{false};std::uint64_t frames=0,invalid=0,updates=0,snapshots=0;std::string last_error;
 void on_connection_epoch(std::uint64_t e)noexcept override{epoch=e;book.begin_recovery();last.clear();}
 void on_frame(std::uint64_t e,std::int64_t receive,std::int64_t,std::string_view payload)noexcept override{++frames;try{boost::system::error_code ec;auto raw=json::parse(payload,ec);if(ec||!raw.is_object()){++invalid;return;}auto&o=raw.as_object();auto*t=field(o,"type");if(text(t,"subscriptions"))return;if(text(t,"error")){++invalid;auto*m=field(o,"message");last_error=m&&m->is_string()?std::string(m->as_string()):"exchange_error";return;}if(e!=epoch){epoch=e;book.begin_recovery();last.clear();}if(text(t,"snapshot")){CoinbaseDepthSnapshot snap;snap.local_receive_monotonic_ns=receive;if(!levels(field(o,"bids"),snap.bids)||!levels(field(o,"asks"),snap.asks)||!book.install_snapshot(snap)){++invalid;return;}++snapshots;live.store(true,std::memory_order_release);record(receive);}else if(text(t,"l2update")){CoinbaseDepthUpdate up;up.local_receive_monotonic_ns=receive;if(!changes(field(o,"changes"),up.changes)){++invalid;return;}if(receive>=begin.load(std::memory_order_acquire)){for(const auto&c:up.changes)changes_seen.push_back({receive,change_key(c)});}if(!book.apply_update(up)){++invalid;return;}++updates;live.store(true,std::memory_order_release);record(receive);}}catch(...){++invalid;}}
private:
 bool levels(const json::value*v,std::vector<CoinbaseDepthLevel>&out){if(!v||!v->is_array()||v->as_array().empty())return false;for(auto&x:v->as_array()){if(!x.is_array()||x.as_array().size()!=2)return false;CoinbaseDepthLevel l;if(!number(&x.as_array()[0],l.price)||!number(&x.as_array()[1],l.quantity)||l.price<=0||l.quantity<=0)return false;out.push_back(l);}return true;}
 bool changes(const json::value*v,std::vector<CoinbaseDepthChange>&out){if(!v||!v->is_array()||v->as_array().empty())return false;for(auto&x:v->as_array()){if(!x.is_array()||x.as_array().size()!=3)return false;CoinbaseDepthChange c;if(text(&x.as_array()[0],"buy"))c.bid=true;else if(text(&x.as_array()[0],"sell"))c.bid=false;else return false;if(!number(&x.as_array()[1],c.price)||!number(&x.as_array()[2],c.quantity)||c.price<=0||c.quantity<0)return false;out.push_back(c);}return true;}
 static std::string change_key(const CoinbaseDepthChange&c){auto q=[](double v){return static_cast<long long>(std::llround(v*100000000.0));};return std::string(c.bid?"B:":"A:")+std::to_string(q(c.price))+":"+std::to_string(q(c.quantity));}
 void record(std::int64_t r){auto m=book.metrics();if(!m.valid)return;auto f=fp(m);if(f==last)return;last=f;if(r>=begin.load(std::memory_order_acquire))states.push_back({r,std::move(f)});}
 std::atomic<std::int64_t>&begin;CoinbaseL2Book book;std::uint64_t epoch=0;std::string last;
};
}
int main(int argc,char**argv){try{int duration=20,warmup=2;bool validate=false;for(int i=1;i<argc;++i){std::string_view a=argv[i];if(a=="--duration-seconds"&&i+1<argc)duration=bounded(argv[++i],1,300);else if(a=="--warmup-seconds"&&i+1<argc)warmup=bounded(argv[++i],1,30);else if(a=="--validate-only")validate=true;else throw std::invalid_argument("unknown/incomplete argument");}auto advanced=coinbase_advanced_level2_connection_spec(500);auto batch=btc_spot_connection_spec(VenueId::CoinbaseSpot,500);if(validate){std::cout<<"Coinbase Advanced public level2 vs Exchange batch configuration PASS\n";return 0;}std::atomic<std::int64_t>begin{std::numeric_limits<std::int64_t>::max()};Advanced ao(begin);ExchangeBatch bo(begin);ExternalVenueWsClient ac(advanced,nullptr,&ao),bc(batch,nullptr,&bo);
#if defined(__APPLE__)
 std::atomic<bool> stopping{false};ExternalStopToken token(stopping);
#else
 std::stop_source stopping;auto token=stopping.get_token();
#endif
 std::thread at([&]{ac.run(token);}),bt([&]{bc.run(token);});auto deadline=now_ns()+15'000'000'000LL;while(now_ns()<deadline&&!(ao.live.load()&&bo.live.load()))std::this_thread::sleep_for(20ms);bool ready=ao.live.load()&&bo.live.load();auto start=now_ns()+static_cast<std::int64_t>(warmup)*1'000'000'000LL;begin.store(start,std::memory_order_release);auto finish=start+static_cast<std::int64_t>(duration)*1'000'000'000LL;while(now_ns()<finish)std::this_thread::sleep_for(20ms);
#if defined(__APPLE__)
 stopping.store(true,std::memory_order_release);
#else
 stopping.request_stop();
#endif
 at.join();bt.join();std::map<std::string,std::deque<std::int64_t>> adv;for(auto&s:ao.states)adv[s.fingerprint].push_back(s.receive_ns);std::vector<double>batch_minus_advanced;std::uint64_t matched=0,batch_first=0;for(auto&s:bo.states){auto&dq=adv[s.fingerprint];if(dq.empty())continue;auto t=dq.front();dq.pop_front();double d=(s.receive_ns-t)/1000.0;batch_minus_advanced.push_back(d);++matched;if(d<0)++batch_first;}std::uint64_t remaining=0;for(auto&[_,q]:adv)remaining+=q.size();std::map<std::string,std::vector<std::int64_t>> acg,bcg;for(const auto&c:ao.changes_seen)acg[c.key].push_back(c.receive_ns);for(const auto&c:bo.changes_seen)bcg[c.key].push_back(c.receive_ns);std::vector<double>change_lag;std::uint64_t unique_matched_changes=0,ambiguous_change_keys=0;for(const auto&[key,av]:acg){auto it=bcg.find(key);if(it==bcg.end())continue;if(av.size()!=1||it->second.size()!=1){++ambiguous_change_keys;continue;}++unique_matched_changes;change_lag.push_back((it->second[0]-av[0])/1000.0);}auto as=ac.snapshot(),bs=bc.snapshot();bool clean=ready&&ao.invalid==0&&bo.invalid==0&&ao.gaps==0&&as.transport_failures==0&&bs.transport_failures==0;std::cout<<json::serialize(json::object{{"schema","polymarket_v7_coinbase_advanced_vs_exchange_batch_v1"},{"paper_only",true},{"authenticated_execution",false},{"real_order_submission",false},{"scope","SAME_HOST_RECONSTRUCTED_BBO_STATE_ARRIVAL_ONLY"},{"clean_capture",clean},{"duration_seconds",duration},{"advanced_bbo_states",ao.states.size()},{"batch_bbo_states",bo.states.size()},{"matched_states",matched},{"advanced_states_not_observed_in_batch_matching",remaining},{"batch_arrived_first_on_matched",batch_first},{"matched_coverage_of_advanced",ao.states.empty()?json::value(nullptr):json::value(static_cast<double>(matched)/ao.states.size())},{"batch_minus_advanced_us",dist(std::move(batch_minus_advanced))},{"advanced_frames",ao.frames},{"batch_frames",bo.frames},{"advanced_updates",ao.updates},{"batch_updates",bo.updates},{"advanced_sequence_gaps",ao.gaps},{"advanced_invalid",ao.invalid},{"batch_invalid",bo.invalid},{"advanced_change_count",ao.changes_seen.size()},{"batch_change_count",bo.changes_seen.size()},{"unique_matched_changes",unique_matched_changes},{"ambiguous_change_keys_excluded",ambiguous_change_keys},{"batch_minus_advanced_unique_change_us",dist(std::move(change_lag))},{"advanced_last_error",ao.last_error},{"batch_last_error",bo.last_error}})<<'\n';return clean?0:2;}catch(const std::exception&e){std::cerr<<"coinbase_advanced_race_probe: "<<e.what()<<'\n';return 64;}}
