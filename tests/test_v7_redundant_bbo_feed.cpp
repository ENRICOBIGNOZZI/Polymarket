#include "pm/v7_redundant_bbo_feed.hpp"
#include <cassert>
#include <vector>
using namespace pm::v7::redundant_bbo;
int main(){
 std::vector<pm::v7::polymarket_bbo::Binding> bindings{{"asset-a",11,21,31},{"asset-b",12,22,32}};
 Feed feed("wss://ws-subscriptions-clob.polymarket.com/ws/market",bindings,Mode::Quorum2Of3);
 const auto before=feed.snapshot(); assert(!before.started && before.disabled_mask==0);
 const auto generations=feed.generations(); assert(generations[0]==0 && generations[1]==0 && generations[2]==0);
 Decision d; assert(!feed.try_next_actionable(d));
 // Constructor/destructor are deliberately network-cold. start() is not called.
 return 0;
}
