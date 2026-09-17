#include "pm/v7_polymarket_bbo.hpp"
#include <array>
#include <cassert>
using namespace pm::v7::polymarket_bbo;
int main(){
 Decoder d({{"asset-a",11,21,31},{"asset-b",12,22,32}}); std::array<Update,8> out{};
 constexpr auto one=R"({"event_type":"best_bid_ask","asset_id":"asset-a","timestamp":123456789001,"best_bid":"0.49","best_ask":"0.51"})";
 auto r=d.decode(one,1100,out); assert(!r.invalid_frame && r.output_count==1 && out[0].best_bid_e4==4900);
 constexpr auto batch=R"([{"event_type":"book","asset_id":"asset-a","timestamp":123456789000},{"event_type":"best_bid_ask","asset_id":"asset-a","timestamp":123456789002,"best_bid":"0.48","best_ask":"0.52"},{"event_type":"price_change","timestamp":"123456789003","price_changes":[{"asset_id":"asset-b","best_bid":"0.3999","best_ask":"0.4001"}]}])";
 r=d.decode(batch,1200,out); assert(!r.invalid_frame && r.output_count==2); assert(out[0].best_bid_e4==4800 && out[1].best_ask_e4==4001);
 return 0;
}
