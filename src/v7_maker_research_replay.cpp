// Offline research adapter over the canonical queue engine. No files, sockets,
// authorizations or canonical ledger writes; one bounded JSON request on stdin.
#include "pm/v7_maker_paper.hpp"
#include <boost/json.hpp>
#include <iostream>
#include <iterator>
#include <cmath>
#include <stdexcept>
namespace j = boost::json;
using namespace pm::v7;
using namespace pm::v7::maker;
static double number(const j::object& x, const char* k) { return j::value_to<double>(x.at(k)); }
static std::int64_t integer(const j::object& x, const char* k) { return j::value_to<std::int64_t>(x.at(k)); }
int main() {
 try {
  const std::string input{std::istreambuf_iterator<char>(std::cin), {}};
  if(input.size()>32*1024*1024) throw std::runtime_error("request too large");
  const auto request=j::parse(input).as_object();
  const auto start=integer(request,"start_ns"), exchange=integer(request,"exchange_ns");
  const double tick=number(request,"tick"), price=number(request,"price"), qty=number(request,"quantity");
  const auto life=integer(request,"lifetime_ms");
  if(start<=0 || exchange<=0 || !(tick>0 && tick<1) || !(price>0 && price<1)
      || !(qty>0 && qty<=1000000) || life<=0 || life>10000
      || number(request,"queue_ahead")<0 || price>=number(request,"best_ask")
      || std::abs(price/tick-std::round(price/tick))>1e-7)
    throw std::runtime_error("invalid bounded post-only research request");
  MakerPaperMarketEngine engine(1,2,3);
  StrategyIntent intent;
  intent.intent_id=1;intent.market_handle=1;intent.event_handle=1;intent.instrument_handle=2;
  intent.strategy_id=StrategyId::ProfessionalMaker;intent.type=IntentType::Quote;intent.side=Side::Buy;
  intent.price_tick=std::llround(price/tick);intent.quantity_microunits=std::llround(qty*1000000);
  intent.decision_monotonic_ns=start;intent.exchange_event_ns=exchange;
  intent.horizon_ms=static_cast<std::uint32_t>(life);intent.passive=1;intent.post_only=1;
  auto result=engine.apply_intent(intent,std::llround(number(request,"queue_ahead")*1000000),std::lround(tick*10000));
  if(!result.applied || result.rejected)throw std::runtime_error("native quote rejected");
  j::array fills;std::int64_t quantity=0;std::uint64_t scenarios=0;int terminal=0;
  auto consume=[&](const PaperMakerResult& r){
   if(r.invariant_violation)throw std::runtime_error("native invariant");
   for(std::size_t i=0;i<r.event_count;++i){const auto& e=r.events[i];
    if(e.kind==PaperMakerEventKind::Fill){++scenarios;if(e.operational_fill_microunits>0){
      quantity+=e.operational_fill_microunits;
      fills.emplace_back(j::object{{"receive_monotonic_ns",e.timestamp_ns},{"quantity",e.operational_fill_microunits/1000000.0},{"price",price},{"trade_id",e.trade_id}});
    }}
    if(e.execution_outcome!=PaperExecutionOutcome::Pending)terminal=static_cast<int>(e.execution_outcome);
   }
  };
  const auto expiry=start+1000000+life*1000000, cancelled=expiry+100000000;
  std::int64_t previous=start;bool expired=false,acked=false;
  for(const auto& v:request.at("trades").as_array()){
   const auto& t=v.as_object();const auto received=integer(t,"receive_monotonic_ns");
   if(received<previous)throw std::runtime_error("receive clock regression");
   previous=received;
   if(received>=expiry && !expired){consume(engine.advance_time(expiry));expired=true;}
   if(received>=cancelled && !acked){consume(engine.advance_time(cancelled));acked=true;}
   PublicTradePrint trade;trade.trade_id=j::value_to<std::uint64_t>(t.at("observer_sequence"));trade.instrument_handle=2;
   trade.aggressor_side=t.at("aggressor_side").as_string()=="SELL"?Side::Sell:Side::Buy;
   const double trade_price=number(t,"price");
   if(std::abs(trade_price/tick-std::round(trade_price/tick))>1e-7)throw std::runtime_error("trade tick changed");
   trade.price_tick=std::llround(trade_price/tick);trade.quantity_microunits=std::llround(number(t,"size")*1000000);
   trade.exchange_event_ns=integer(t,"exchange_event_ns");trade.receive_monotonic_ns=received;
   consume(engine.on_public_trade(trade));
  }
  if(!expired)consume(engine.advance_time(expiry));
  if(!acked)consume(engine.advance_time(cancelled));
  std::cout<<j::serialize(j::object{{"schema","polymarket_v7_maker_research_replay_v1"},{"paper_only",true},
   {"authenticated_execution",false},{"real_order_submission",false},{"execution_authority","ZERO_AUTHORITY_RESEARCH_ONLY"},
   {"excluded_from_portfolio_equity",true},{"simulation_fill_events",scenarios},{"operational_filled_shares",quantity/1000000.0},
   {"fills",std::move(fills)},{"execution_outcome",terminal},{"active_orders",engine.active_order_count()}})<<'\n';
  return 0;
 }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 2;}
}
