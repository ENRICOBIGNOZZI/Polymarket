#include "pm/v7_coinbase_l2.hpp"
#include "pm/v7_external_ws.hpp"

#include <boost/json.hpp>
#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <deque>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <thread>
#include <tuple>
#include <vector>
#if !defined(__APPLE__)
#include <stop_token>
#endif

namespace json = boost::json;
using namespace pm::v7::external_fair;
using namespace std::chrono_literals;
namespace {
struct State { std::int64_t receive_ns=0; std::string fingerprint; };
std::int64_t now_ns(){return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
int bounded(std::string_view t,int lo,int hi){int v=0;auto r=std::from_chars(t.data(),t.data()+t.size(),v);if(r.ec!=std::errc{}||r.ptr!=t.data()+t.size()||v<lo||v>hi)throw std::invalid_argument("bounded integer argument required");return v;}
const json::value* field(const json::object& o,std::string_view key){auto it=o.find(key);return it==o.end()?nullptr:&it->value();}
bool number(const json::value* v,double& out){if(!v)return false;try{if(v->is_string())out=std::stod(std::string(v->as_string()));else if(v->is_double())out=v->as_double();else if(v->is_int64())out=static_cast<double>(v->as_int64());else if(v->is_uint64())out=static_cast<double>(v->as_uint64());else return false;return std::isfinite(out);}catch(...){return false;}}
bool text(const json::value* v,std::string_view expected){return v&&v->is_string()&&v->as_string()==expected;}
std::string fingerprint(const CoinbaseL2Metrics&m){auto q=[](double v){return static_cast<long long>(std::llround(v*100000000.0));};return std::to_string(q(m.best_bid))+":"+std::to_string(q(m.bid_depth_l1))+":"+std::to_string(q(m.best_ask))+":"+std::to_string(q(m.ask_depth_l1));}
json::object dist(std::vector<double> v){if(v.empty())return{{"count",0},{"p50_us",nullptr},{"p95_us",nullptr},{"p99_us",nullptr},{"min_us",nullptr},{"max_us",nullptr}};std::sort(v.begin(),v.end());auto q=[&](double p){return v[static_cast<std::size_t>(std::ceil(p*v.size()))-1];};return{{"count",v.size()},{"p50_us",q(.5)},{"p95_us",q(.95)},{"p99_us",q(.99)},{"min_us",v.front()},{"max_us",v.back()}};}
class Observer final:public ExternalFrameObserver{
public:
 explicit Observer(std::atomic<std::int64_t>&begin):begin_(begin){}
 std::vector<State> states; std::atomic<bool> live{false}; std::uint64_t frames=0,invalid=0,snapshots=0,updates=0,bbo_changes=0; std::string last_error;
 void on_connection_epoch(std::uint64_t e)noexcept override{epoch_=e;book_.begin_recovery();last_.clear();}
 void on_frame(std::uint64_t e,std::int64_t receive,std::int64_t,std::string_view payload)noexcept override{
  ++frames;try{boost::system::error_code ec;auto raw=json::parse(payload,ec);if(ec||!raw.is_object()){++invalid;return;}auto&o=raw.as_object();auto*t=field(o,"type");if(text(t,"subscriptions"))return;if(text(t,"error")){++invalid; auto*m=field(o,"message"); last_error=(m&&m->is_string())?std::string(m->as_string()):"exchange_error"; return;}if(e!=epoch_){epoch_=e;book_.begin_recovery();last_.clear();}
   if(text(t,"snapshot")){CoinbaseDepthSnapshot s;s.local_receive_monotonic_ns=receive;if(!levels(field(o,"bids"),s.bids)||!levels(field(o,"asks"),s.asks)||!book_.install_snapshot(s)){++invalid;return;}++snapshots;live.store(true,std::memory_order_release);record(receive);return;}
   if(text(t,"l2update")){CoinbaseDepthUpdate u;u.local_receive_monotonic_ns=receive;if(!changes(field(o,"changes"),u.changes)||!book_.apply_update(u)){++invalid;return;}++updates;live.store(true,std::memory_order_release);record(receive);return;}
  }catch(...){++invalid;}
 }
private:
 bool levels(const json::value*v,std::vector<CoinbaseDepthLevel>&out){if(!v||!v->is_array()||v->as_array().empty())return false;for(auto&x:v->as_array()){if(!x.is_array()||x.as_array().size()!=2)return false;CoinbaseDepthLevel l;if(!number(&x.as_array()[0],l.price)||!number(&x.as_array()[1],l.quantity)||l.price<=0||l.quantity<=0)return false;out.push_back(l);}return true;}
 bool changes(const json::value*v,std::vector<CoinbaseDepthChange>&out){if(!v||!v->is_array()||v->as_array().empty())return false;for(auto&x:v->as_array()){if(!x.is_array()||x.as_array().size()!=3)return false;CoinbaseDepthChange c;if(text(&x.as_array()[0],"buy"))c.bid=true;else if(text(&x.as_array()[0],"sell"))c.bid=false;else return false;if(!number(&x.as_array()[1],c.price)||!number(&x.as_array()[2],c.quantity)||c.price<=0||c.quantity<0)return false;out.push_back(c);}return true;}
 void record(std::int64_t receive){auto m=book_.metrics();if(!m.valid)return;auto f=fingerprint(m);if(f==last_)return;last_=f;++bbo_changes;if(receive>=begin_.load(std::memory_order_acquire))states.push_back({receive,std::move(f)});}
 std::atomic<std::int64_t>&begin_;CoinbaseL2Book book_;std::uint64_t epoch_=0;std::string last_;
};
}
int main(int argc,char**argv){try{int duration=60,warmup=3;bool validate=false;for(int i=1;i<argc;++i){std::string_view a=argv[i];if(a=="--duration-seconds"&&i+1<argc)duration=bounded(argv[++i],1,300);else if(a=="--warmup-seconds"&&i+1<argc)warmup=bounded(argv[++i],1,30);else if(a=="--validate-only")validate=true;else throw std::invalid_argument("unknown/incomplete argument");}
 auto instant=coinbase_level2_connection_spec(500);auto batch=btc_spot_connection_spec(VenueId::CoinbaseSpot,500);if(validate){std::cout<<"Coinbase public level2/batch race configuration PASS\n";return 0;}
 std::atomic<std::int64_t>begin{std::numeric_limits<std::int64_t>::max()};Observer io(begin),bo(begin);ExternalVenueWsClient ic(instant,nullptr,&io),bc(batch,nullptr,&bo);
#if defined(__APPLE__)
 std::atomic<bool> stopping{false};ExternalStopToken token(stopping);
#else
 std::stop_source stopping;auto token=stopping.get_token();
#endif
 std::thread it([&]{ic.run(token);}),bt([&]{bc.run(token);});auto deadline=now_ns()+15'000'000'000LL;while(now_ns()<deadline&&!(io.live.load()&&bo.live.load()))std::this_thread::sleep_for(20ms);bool ready=io.live.load()&&bo.live.load();auto start=now_ns()+static_cast<std::int64_t>(warmup)*1'000'000'000LL;begin.store(start,std::memory_order_release);auto finish=start+static_cast<std::int64_t>(duration)*1'000'000'000LL;while(now_ns()<finish)std::this_thread::sleep_for(20ms);
#if defined(__APPLE__)
 stopping.store(true,std::memory_order_release);
#else
 stopping.request_stop();
#endif
 it.join();bt.join();std::map<std::string,std::deque<std::int64_t>> instant_by_state;for(auto&s:io.states)instant_by_state[s.fingerprint].push_back(s.receive_ns);std::vector<double> lag;std::uint64_t matched=0,batch_before=0;for(auto&s:bo.states){auto&dq=instant_by_state[s.fingerprint];if(dq.empty())continue;auto t=dq.front();dq.pop_front();double us=(s.receive_ns-t)/1000.0;lag.push_back(us);++matched;if(us<0)++batch_before;}std::uint64_t remaining=0;for(auto&[_,q]:instant_by_state)remaining+=q.size();auto is=ic.snapshot(),bs=bc.snapshot();bool clean=ready&&io.invalid==0&&bo.invalid==0&&is.transport_failures==0&&bs.transport_failures==0;
 std::cout<<json::serialize(json::object{{"schema","polymarket_v7_coinbase_level2_batch_race_v1"},{"paper_only",true},{"authenticated_execution",false},{"real_order_submission",false},{"scope","SAME_HOST_RECONSTRUCTED_BBO_STATE_ARRIVAL_ONLY"},{"clean_capture",clean},{"duration_seconds",duration},{"level2_bbo_states",io.states.size()},{"batch_bbo_states",bo.states.size()},{"matched_states",matched},{"level2_states_not_observed_in_batch_matching",remaining},{"batch_arrived_before_level2_on_matched",batch_before},{"matched_coverage_of_level2",io.states.empty()?json::value(nullptr):json::value(static_cast<double>(matched)/io.states.size())},{"batch_minus_level2_us",dist(std::move(lag))},{"level2_frames",io.frames},{"batch_frames",bo.frames},{"level2_updates",io.updates},{"batch_updates",bo.updates},{"level2_invalid",io.invalid},{"batch_invalid",bo.invalid},{"level2_last_error",io.last_error},{"batch_last_error",bo.last_error}})<<'\n';return clean?0:2;}catch(const std::exception&e){std::cerr<<"coinbase_level2_race_probe: "<<e.what()<<'\n';return 64;}}
