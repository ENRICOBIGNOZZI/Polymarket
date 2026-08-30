#include "pm/v7_maker_paper.hpp"

#include <cmath>
#include <limits>

namespace pm::v7::maker {

PaperMakerResult MakerPaperMarketEngine::split_complete_sets(
    SleeveCapitalAccount& capital,
    std::int64_t microunits,
    std::int64_t timestamp_ns,
    double yes_reference_price) noexcept {

    PaperMakerResult result;
    const auto before = inventory_;
    const auto capital_before = capital.inventory_committed_for_market(market_handle_);
    PaperMakerEvent requested;
    requested.kind = PaperMakerEventKind::InventorySplitRequested;
    requested.timestamp_ns = timestamp_ns;
    requested.original_microunits = microunits;
    requested.yes_before_microunits = before.yes_microunits;
    requested.no_before_microunits = before.no_microunits;
    requested.yes_after_microunits = before.yes_microunits;
    requested.no_after_microunits = before.no_microunits;
    requested.yes_cost_before = before.yes_cost;
    requested.no_cost_before = before.no_cost;
    requested.yes_cost_after = before.yes_cost;
    requested.no_cost_after = before.no_cost;
    requested.collateral_before_microdollars = capital_before;
    requested.collateral_after_microdollars = capital_before;
    requested.split_yes_reference_price = yes_reference_price;
    emit(result, requested);

    auto reject = [&]() noexcept {
        result.rejected = 1;
        PaperMakerEvent event;
        event.kind = PaperMakerEventKind::InventorySplitRejected;
        event.timestamp_ns = timestamp_ns;
        event.original_microunits = microunits;
        event.yes_before_microunits = before.yes_microunits;
        event.no_before_microunits = before.no_microunits;
        event.yes_after_microunits = inventory_.yes_microunits;
        event.no_after_microunits = inventory_.no_microunits;
        event.yes_cost_before = before.yes_cost;
        event.no_cost_before = before.no_cost;
        event.yes_cost_after = inventory_.yes_cost;
        event.no_cost_after = inventory_.no_cost;
        event.collateral_before_microdollars = capital_before;
        event.collateral_after_microdollars =
            capital.inventory_committed_for_market(market_handle_);
        event.split_yes_reference_price = yes_reference_price;
        emit(result, event);
        return result;
    };

    if (!policy_.valid() || microunits <= 0 || timestamp_ns <= 0
        || !std::isfinite(yes_reference_price)
        || yes_reference_price <= 0.0 || yes_reference_price >= 1.0) return reject();
    if (inventory_.yes_microunits > std::numeric_limits<std::int64_t>::max() - microunits
        || inventory_.no_microunits > std::numeric_limits<std::int64_t>::max() - microunits) {
        result.invariant_violation = 1;
        return reject();
    }

    constexpr double kMicrounitsPerShare = 1'000'000.0;
    const double split_shares = static_cast<double>(microunits) / kMicrounitsPerShare;
    const double yes_leg_cost = yes_reference_price * split_shares;
    const double no_leg_cost = (1.0 - yes_reference_price) * split_shares;
    if (!std::isfinite(split_shares) || split_shares <= 0.0
        || !std::isfinite(yes_leg_cost) || !std::isfinite(no_leg_cost)
        || !std::isfinite(inventory_.yes_cost + yes_leg_cost)
        || !std::isfinite(inventory_.no_cost + no_leg_cost)) {
        result.invariant_violation = 1;
        return reject();
    }

    // A complete-set share costs exactly one dollar of collateral.  Share
    // microunits and collateral microdollars therefore have the same integer
    // magnitude.  Fail closed before mutating inventory if the common V7
    // capital plane cannot reserve the transformation.
    if (!capital.commit_inventory(market_handle_, microunits)) return reject();

    inventory_.pending_split_microunits = microunits;
    inventory_.yes_microunits += microunits;
    inventory_.no_microunits += microunits;
    inventory_.yes_cost += yes_leg_cost;
    inventory_.no_cost += no_leg_cost;
    inventory_.pending_split_microunits = 0;
    refresh_inventory_derived();

    PaperMakerEvent event;
    event.kind = PaperMakerEventKind::InventorySplit;
    event.timestamp_ns = timestamp_ns;
    event.original_microunits = microunits;
    event.operational_fill_microunits = microunits;
    event.realized_pnl = 0.0;
    event.yes_before_microunits = before.yes_microunits;
    event.no_before_microunits = before.no_microunits;
    event.yes_after_microunits = inventory_.yes_microunits;
    event.no_after_microunits = inventory_.no_microunits;
    event.yes_cost_before = before.yes_cost;
    event.no_cost_before = before.no_cost;
    event.yes_cost_after = inventory_.yes_cost;
    event.no_cost_after = inventory_.no_cost;
    event.collateral_before_microdollars = capital_before;
    event.collateral_after_microdollars =
        capital.inventory_committed_for_market(market_handle_);
    event.split_yes_reference_price = yes_reference_price;
    emit(result, event);
    result.applied = 1;
    return result;
}

} // namespace pm::v7::maker
