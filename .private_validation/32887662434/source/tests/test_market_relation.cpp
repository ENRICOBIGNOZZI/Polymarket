#include "pm/market_relation.hpp"

#include <cassert>
#include <iostream>

int main() {
    pm::Market score_a;
    score_a.event_id = "event-1";
    score_a.slug = "sea-bol-laz-2026-08-24-exact-score-1-3";
    pm::Market score_b;
    score_b.event_id = "event-1";
    score_b.slug = "sea-bol-laz-2026-08-24-exact-score-2-3";
    assert(pm::market_relation(score_a, score_b) == pm::MarketRelation::same_event);

    pm::Market election_a;
    election_a.slug = "will-andy-beshear-win-the-2028-us-presidential-election";
    pm::Market election_b;
    election_b.slug = "will-gavin-newsom-win-the-2028-us-presidential-election";
    assert(pm::market_relation(election_a, election_b) == pm::MarketRelation::semantic);

    pm::Market pandemic;
    pandemic.slug = "hantavirus-pandemic-in-2026";
    assert(pm::market_relation(election_a, pandemic) == pm::MarketRelation::none);

    pm::Market soccer;
    soccer.slug = "sea-bol-laz-2026-08-24-draw";
    assert(pm::market_relation(election_a, soccer) == pm::MarketRelation::none);

    // Calendar numbers alone must not manufacture a semantic relation.
    assert(pm::relation_tokens(election_a).count("2028") == 0);
    assert(pm::relation_tokens(pandemic).count("2026") == 0);

    std::cout << "market relation tests passed\n";
    return 0;
}
