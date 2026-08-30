#include "pm/v7_maker_paper.hpp"

#include <cassert>
#include <cmath>

namespace {

constexpr std::uint64_t kMarket = 11;
constexpr std::uint64_t kYes = 101;
constexpr std::uint64_t kNo = 102;

pm::v7::CapitalLimits capital_limits(std::int64_t total = 5'000'000) {
    pm::v7::CapitalLimits limits;
    limits.sleeve_budget_microdollars = total;
    limits.max_total_exposure_microdollars = total;
    limits.max_market_exposure_microdollars = total;
    limits.max_single_order_microdollars = total;
    return limits;
}

pm::v7::StrategyIntent sell_yes(std::uint64_t id, std::int64_t quantity) {
    pm::v7::StrategyIntent intent;
    intent.intent_id = id;
    intent.market_handle = kMarket;
    intent.event_handle = 22;
    intent.instrument_handle = kYes;
    intent.strategy_id = pm::v7::StrategyId::ProfessionalMaker;
    intent.type = pm::v7::IntentType::Quote;
    intent.side = pm::v7::Side::Sell;
    intent.price_tick = 52;
    intent.quantity_microunits = quantity;
    intent.decision_monotonic_ns = 1'100'000'000LL;
    intent.exchange_event_ns = 10'100'000'000LL;
    intent.passive = 1;
    intent.post_only = 1;
    return intent;
}

void test_split_is_explicit_balanced_and_auditable() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    const auto split = engine.split_complete_sets(capital, 2'000'000, 1'000'000'000LL);
    assert(split.applied);
    assert(!split.rejected);
    assert(split.event_count == 2);
    assert(split.events[0].kind
           == pm::v7::maker::PaperMakerEventKind::InventorySplitRequested);
    assert(split.events[1].kind == pm::v7::maker::PaperMakerEventKind::InventorySplit);
    assert(split.events[1].operational_fill_microunits == 2'000'000);
    assert(split.events[1].yes_before_microunits == 0);
    assert(split.events[1].yes_after_microunits == 2'000'000);
    assert(split.events[1].collateral_before_microdollars == 0);
    assert(split.events[1].collateral_after_microdollars == 2'000'000);

    const auto& inventory = engine.inventory();
    assert(inventory.yes_microunits == 2'000'000);
    assert(inventory.no_microunits == 2'000'000);
    assert(inventory.yes_free_microunits == 2'000'000);
    assert(inventory.no_free_microunits == 2'000'000);
    assert(inventory.balanced_complete_set_microunits == 2'000'000);
    assert(std::abs(inventory.yes_cost - 1.0) < 1e-12);
    assert(std::abs(inventory.no_cost - 1.0) < 1e-12);
    assert(capital.inventory_committed_for_market(kMarket) == 2'000'000);
    assert(capital.snapshot().order_reserved_microdollars == 0);
}

void test_split_inventory_can_back_post_only_ask() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    assert(engine.split_complete_sets(capital, 1'000'000, 1'000'000'000LL).applied);
    const auto result = engine.apply_intent(sell_yes(1, 1'000'000), 2'000'000, 100);
    assert(result.applied);
    assert(!result.rejected);
    assert(engine.active_order_count() == 1);
    assert(engine.inventory().yes_reserved_sell_microunits == 1'000'000);
    assert(engine.inventory().yes_free_microunits == 0);
}

void test_split_rejects_invalid_quantity_without_mutating_inventory() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    pm::v7::SleeveCapitalAccount capital(capital_limits());
    const auto result = engine.split_complete_sets(capital, 0, 1'000'000'000LL);
    assert(result.rejected);
    assert(engine.inventory().yes_microunits == 0);
    assert(engine.inventory().no_microunits == 0);
    assert(engine.inventory().yes_cost == 0.0);
    assert(engine.inventory().no_cost == 0.0);
    assert(capital.snapshot().total_exposure_microdollars == 0);
    assert(result.event_count == 2);
    assert(result.events[1].kind
           == pm::v7::maker::PaperMakerEventKind::InventorySplitRejected);
}

void test_split_fails_closed_when_common_capital_denies() {
    pm::v7::maker::MakerPaperMarketEngine engine(kMarket, kYes, kNo);
    pm::v7::SleeveCapitalAccount capital(capital_limits(500'000));
    const auto result = engine.split_complete_sets(capital, 1'000'000, 1'000'000'000LL);
    assert(result.rejected);
    assert(!result.applied);
    assert(engine.inventory().yes_microunits == 0);
    assert(engine.inventory().no_microunits == 0);
    assert(engine.inventory().yes_cost == 0.0);
    assert(engine.inventory().no_cost == 0.0);
    assert(capital.snapshot().total_exposure_microdollars == 0);
}

} // namespace

int main() {
    test_split_is_explicit_balanced_and_auditable();
    test_split_inventory_can_back_post_only_ask();
    test_split_rejects_invalid_quantity_without_mutating_inventory();
    test_split_fails_closed_when_common_capital_denies();
    return 0;
}
