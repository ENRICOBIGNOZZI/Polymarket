#include "pm/v7_multirate_context.hpp"
#include "pm/v7_external_state.hpp"
#include <boost/json.hpp>
#include <cassert>
#include <thread>
using namespace pm::v7;

SlowContextSnapshot sample(std::uint64_t version, std::int64_t received = 100) {
    SlowContextSnapshot out;
    out.version=version; out.published_ns=200; out.envelope_valid=1; out.valid_mask=3;
    out.fields[0]={42.,received,300,1}; out.fields[1]={.1,received,250,2}; return out;
}
void test_independent_freshness_and_no_wait() {
    SlowContextCache cache;
    assert(cache.at(201).satisfies(0));
    assert(!cache.at(201).satisfies(1));
    assert(cache.apply(sample(1),200));
    assert(cache.at(250).fresh_mask==3);
    assert(cache.at(251).fresh_mask==1);
    assert(cache.at(301).fresh_mask==0);
    auto heartbeat=sample(2);heartbeat.published_ns=290;
    assert(cache.apply(heartbeat,290));
    assert(cache.at(291).fresh_mask==1); // publication did not rejuvenate field 1
    assert(cache.at(301).fresh_mask==0);
    assert(!cache.apply(sample(1),300)); // replay cannot roll the state back
    auto wrong=sample(3);wrong.published_ns=500;
    assert(!cache.apply(wrong,300));assert(cache.at(300).fresh_mask==0);
    assert(cache.at(300).satisfies(0)); // independent fast model remains eligible
    assert(!cache.at(300).satisfies(1U<<30));
}
void test_future_fields_and_missing_values() {
    SlowContextCache cache;auto out=sample(1);out.fields[0].receive_ns=201;
    assert(cache.apply(out,200));assert(cache.at(220).fresh_mask==2);
    out=sample(2);out.fields[0].value=std::numeric_limits<double>::quiet_NaN();
    assert(cache.apply(out,220));assert(cache.at(220).fresh_mask==2);
    out=sample(3);out.envelope_valid=0;
    assert(!cache.apply(out,220));assert(cache.at(220).fresh_mask==0);
}
void test_bounded_mailbox_overflow_revokes_context() {
    SlowContextMailbox box;SlowContextCache cache;
    for(unsigned i=1;i<=8;++i)assert(box.publish(sample(i)));
    assert(!box.publish(sample(9)));assert(box.faults()==1);
    assert(box.consume(cache,220)==8);assert(cache.at(220).fresh_mask==0);
    assert(box.publish(sample(10)));assert(box.consume(cache,220)==1);
    assert(cache.at(220).fresh_mask==3);
}
void test_publisher_reader_ownership() {
    SlowContextMailbox box;SlowContextCache cache;std::atomic<bool> done{false};
    std::thread producer([&]{for(unsigned i=1;i<=100000;++i)(void)box.publish(sample(i));done.store(true);});
    while(!done.load())(void)box.consume(cache,220);
    producer.join();(void)box.consume(cache,220);
    assert(box.publish(sample(100001)));(void)box.consume(cache,220);
    assert(cache.at(220).snapshot.version==100001);
    assert(cache.at(220).snapshot.fields[0].value==42.);
}
void test_decoder_identity_and_causality() {
    namespace json=boost::json;
    SlowContextIdentity id{"sha","run","market","BTC","M5"};
    json::object fields;for(auto name:kSlowContextNames)fields[std::string(name)]=nullptr;
    fields["spot_composite"]=json::object{{"value",42.},{"receive_monotonic_ns",100},
        {"expires_monotonic_ns",300},{"source_version",1}};
    json::object o{{"schema","polymarket_v7_slow_context_v1"},{"code_sha","sha"},
        {"run_id","run"},{"market_id","market"},{"asset","BTC"},{"horizon","M5"},
        {"paper_only",true},{"authenticated_execution",false},{"real_order_submission",false},
        {"observation_only",true},{"published_monotonic_ns",200},{"version",1},{"fields",fields}};
    auto good=decode_slow_context(json::serialize(o),id,220);
    assert(good.envelope_valid && good.valid_mask==1);
    o["market_id"]="different";bool rejected=false;
    try{(void)decode_slow_context(json::serialize(o),id,220);}catch(...){rejected=true;}assert(rejected);
    o["market_id"]="market";o["published_monotonic_ns"]=221;rejected=false;
    try{(void)decode_slow_context(json::serialize(o),id,220);}catch(...){rejected=true;}assert(rejected);
}
void test_derivative_fields_do_not_share_a_freshness_clock() {
    using namespace pm::v7::external_fair;
    ExternalAssetState state(1);ExternalStatePolicy policy;
    ExternalVenueEvent e;e.asset_handle=1;e.venue=VenueId::BinanceUsdM;e.connection_epoch=1;
    e.event_type=ExternalEventType::DerivativeContext;e.healthy=1;
    e.local_receive_monotonic_ns=1'000'000'000;e.context_valid_mask=DerivativeContextOpenInterest;e.open_interest=5;
    assert(state.on_venue_event(e,policy));
    e.local_receive_monotonic_ns=2'100'000'000;e.context_valid_mask=DerivativeContextFundingRate;e.funding_rate=.001;
    assert(state.on_venue_event(e,policy));
    const auto snapshot=state.snapshot(e.local_receive_monotonic_ns,policy);
    const auto& derivative=snapshot.derivative_contexts[2];
    assert((derivative.valid_mask & DerivativeContextFundingRate)!=0);
    assert((derivative.valid_mask & DerivativeContextOpenInterest)==0);
    assert(state.derivative_field_clocks(VenueId::BinanceUsdM)[3]==1'000'000'000);
}
void test_risk_off_has_no_slow_state_and_never_requotes_toxic_side() {
    assert(fresh_shock_direction(1,100,200,150)==1);
    assert(fresh_shock_direction(-1,100,200,150)==-1);
    assert(fresh_shock_direction(1,100,200,201)==0);
    assert(fresh_shock_direction(1,151,200,150)==0);
    assert(fresh_shock_direction(0,100,200,150)==0);
    for (bool yes : {false,true}) for (int direction : {-1,1}) {
        const auto toxic = (yes == (direction>0)) ? Side::Sell : Side::Buy;
        const auto safe = toxic==Side::Buy ? Side::Sell : Side::Buy;
        assert(quote_is_adverse_to_shock(yes,toxic,direction));
        assert(!quote_is_adverse_to_shock(yes,safe,direction));
        assert(!quote_is_adverse_to_shock(yes,toxic,0));
    }
}
int main(){test_risk_off_has_no_slow_state_and_never_requotes_toxic_side();test_independent_freshness_and_no_wait();test_future_fields_and_missing_values();
    test_bounded_mailbox_overflow_revokes_context();test_publisher_reader_ownership();
    test_decoder_identity_and_causality();test_derivative_fields_do_not_share_a_freshness_clock();}
