#pragma once

// Promotion contract for the unified exact-arbitrage graph.  This header is
// deliberately independent from JSON/control-plane code: graph generations
// are compiled off-path into fixed handles before entering this evaluator.

#include "pm/v7_market_state.hpp"
#include "pm/v7_pure_arb_lane.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <numeric>
#include <span>

namespace pm::v7::exact_arb_graph {

inline constexpr std::size_t kMaxLegs = 16;
inline constexpr std::size_t kMaxDependenciesPerToken = 64;
inline constexpr std::int64_t kShareMicrounits = 1'000'000;

enum class HotReject : std::uint8_t {
    Accepted = 0,
    InvalidRelation,
    MissingBook,
    IncompleteDepth,
    StaleBook,
    LegSkew,
    InsufficientDepth,
    MinimumOrder,
    NoPositiveEdge,
    InventoryUnavailable,
    CapitalLimit,
    UnknownFee,
    NumericOverflow,
    TransformationUnavailable,
    VenuePrecision,
    SizingIncomplete,
};

struct RationalCoefficient {
    std::int64_t numerator = 0;
    std::int64_t denominator = 1;
    [[nodiscard]] bool valid() const noexcept {
        return numerator > 0 && denominator > 0;
    }
    [[nodiscard]] double value() const noexcept {
        return static_cast<double>(numerator) / static_cast<double>(denominator);
    }
};

struct CompiledLeg {
    std::uint32_t book_handle = 0;
    RationalCoefficient coefficient{};
    std::int64_t minimum_order_microunits = 0;
    // These values are pre-verified and precompiled by the control plane. A
    // relation with an unknown fee does not enter this representation.
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
    std::uint8_t fee_verified = 0;
};

struct CompiledNode {
    std::uint32_t book_handle = 0;
    std::array<std::uint8_t, 32> identity_hash{};
};

struct CompiledTransformation {
    std::uint32_t resource_handle = 0;
    std::int64_t capacity_microunits = 0;
    std::int64_t latency_ns = 0;
    std::int64_t capital_lock_ns = 0;
    std::uint8_t verified = 0;
};

struct ResourceDependency {
    std::uint32_t resource_handle = 0;
    std::int64_t units_per_relation_microunits = 0;
};

struct CompiledRelation {
    std::uint64_t relation_handle = 0;
    std::uint64_t proof_handle = 0;
    std::int64_t guaranteed_payout_microunits = 0;
    std::int64_t reserve_per_unit_microunits = 0;
    std::uint8_t leg_count = 0;
    std::uint8_t enabled = 0;
    std::uint8_t sell_inventory = 0;
    std::array<CompiledLeg, kMaxLegs> legs{};
};

struct TokenDependency {
    std::uint32_t token_handle = 0;
    std::uint32_t first_relation = 0;
    std::uint16_t relation_count = 0;
};

struct HotDecision {
    HotReject reject = HotReject::InvalidRelation;
    std::uint64_t relation_handle = 0;
    std::int64_t quantity_microunits = 0;
    std::int64_t gross_pnl_microunits = 0;
    std::int64_t net_pnl_microunits = 0;
    std::int64_t raw_pnl_microunits = 0;
    std::int64_t fees_microunits = 0;
    std::int64_t capital_required_microunits = 0;
    std::array<std::uint16_t, kMaxLegs> levels_consumed{};
    std::uint16_t levels_used = 0;
};

struct HotResources {
    std::int64_t capital_microunits = std::numeric_limits<std::int64_t>::max();
    // Relation-unit sizing limit, not a synthetic transformation or inventory.
    std::int64_t quantity_limit_microunits = std::numeric_limits<std::int64_t>::max();
    // Indexed by book handle, in actual leg shares. Empty means no inventory.
    std::span<const std::int64_t> inventory{};
    const CompiledTransformation* transformation = nullptr;
};

struct GraphGeneration {
    std::array<std::uint8_t, 32> digest{};
    std::span<const CompiledNode> nodes{};
    std::span<const CompiledRelation> relations{};
    std::span<const TokenDependency> dependencies{};
    std::span<const std::uint32_t> relation_handles{};
    // Owner validates and pins all backing storage before swapping this view
    // at an event boundary. This non-owning representation never allocates.
};

struct HotTimingContext {
    // Zero disables the corresponding check for deterministic/offline unit
    // tests. Production callers supply the causal receive clock explicitly.
    std::int64_t now_receive_monotonic_ns = 0;
    std::int64_t maximum_book_age_ns = 0;
    std::int64_t maximum_leg_skew_ns = 0;
};

[[nodiscard]] inline double level_price(const PriceLevelE4& level) noexcept {
    return static_cast<double>(level.price_e4) / 10'000.0;
}

[[nodiscard]] inline bool valid_book(const BookDeepSnapshot& book, bool buy = true) noexcept {
    const auto count = buy ? book.ask_level_count : book.bid_level_count;
    const auto& levels = buy ? book.ask_levels : book.bid_levels;
    if (book.valid == 0 || book.lineage_continuous == 0 || (buy ? book.ask_truncated : book.bid_truncated)
        || count == 0 || count > levels.size()) return false;
    for (std::size_t i = 0; i < count; ++i) {
        if (levels[i].price_e4 <= 0 || levels[i].price_e4 >= 10000 || levels[i].quantity_microunits <= 0) return false;
        if (i && (buy ? levels[i].price_e4 <= levels[i-1].price_e4 : levels[i].price_e4 >= levels[i-1].price_e4)) return false;
    }
    return true;
}

// Exact arithmetic for the recorded 5dp fee model and supported rate lattice /
// exponents. This is not an attestation of venue tie or fill-fragment semantics.
// The frozen champion remains untouched: binary floating-point ties in its
// std::round path are not copied into the graph's accounting oracle.
[[nodiscard]] inline bool valid_fee_terms(const CompiledLeg& leg) noexcept {
    return leg.fee_verified && std::isfinite(leg.fee_rate) && leg.fee_rate >= 0 && leg.fee_rate <= 1
        && std::isfinite(leg.fee_exponent) && leg.fee_exponent >= 0 && leg.fee_exponent <= 2
        && std::abs(leg.fee_rate*1e9-std::round(leg.fee_rate*1e9)) <= 1e-7
        && std::floor(leg.fee_exponent) == leg.fee_exponent;
}

struct RoundedFeeUnits {
    unsigned __int128 units = 0; // 1e-5 PUSD fee units
    bool valid = true;
};

[[nodiscard]] inline RoundedFeeUnits rounded_fee_units(std::int64_t shares_micro, std::int32_t price_e4,
                                                      const CompiledLeg& leg) noexcept {
    if (shares_micro < 0 || price_e4 <= 0 || price_e4 >= 10000 || !valid_fee_terms(leg)) return {0, false};
    if (leg.fee_rate == 0.0) return {};
    using Wide = unsigned __int128;
    const auto rate = static_cast<std::uint64_t>(std::llround(leg.fee_rate * 1e9));
    Wide numerator = static_cast<Wide>(shares_micro) * rate;
    Wide denominator = 10000000000ULL; // microshares * nanorate / fee 1e-5 units
    constexpr Wide maximum = ~static_cast<Wide>(0);
    for (int i=0; i<static_cast<int>(leg.fee_exponent); ++i) {
        const auto factor = static_cast<std::uint64_t>(price_e4) * (10000-price_e4);
        if (numerator > maximum/factor || denominator > maximum/100000000ULL)
            return {0, false};
        numerator *= factor; denominator *= 100000000ULL;
    }
    if (numerator < denominator) return {};
    const Wide whole = numerator/denominator;
    const Wide remainder = numerator%denominator;
    const Wide rounded = whole + (remainder >= (denominator+1)/2 ? 1 : 0);
    return {rounded, true};
}

[[nodiscard]] inline double exact_fee(std::int64_t shares_micro, std::int32_t price_e4,
                                     const CompiledLeg& leg) noexcept {
    const auto rounded = rounded_fee_units(shares_micro, price_e4, leg);
    return rounded.valid ? static_cast<double>(rounded.units)/100000.0 : std::numeric_limits<double>::quiet_NaN();
}

// Legacy full-depth marginal sweep, retained for baseline comparisons. It is
// NOT a global quantity optimizer under rounded fees and fragments fees at
// basket breakpoints. The native research runtime uses order_sizing.hpp.
// Every loop advances at least one level, bounded by total book depth.
[[nodiscard]] inline HotDecision evaluate_basket(
    const CompiledRelation& relation, std::span<const BookDeepSnapshot> books,
    bool buy, HotTimingContext timing = {}, HotResources resources = {}) noexcept {
    HotDecision out{};
    out.relation_handle = relation.relation_handle;
    if (relation.enabled == 0 || relation.leg_count == 0 || relation.leg_count > kMaxLegs
        || relation.guaranteed_payout_microunits <= 0 || relation.reserve_per_unit_microunits < 0) return out;

    std::int64_t capacity = resources.quantity_limit_microunits;
    if (capacity < 0) return out;
    if (resources.transformation) {
        const auto& t = *resources.transformation;
        if (!t.verified || t.capacity_microunits <= 0 || t.latency_ns < 0 || t.capital_lock_ns <= 0) {
            out.reject = HotReject::TransformationUnavailable; return out;
        }
        capacity = std::min(capacity, t.capacity_microunits);
    }
    if (resources.capital_microunits < 0) { out.reject = HotReject::CapitalLimit; return out; }

    std::array<std::size_t, kMaxLegs> level{};
    std::array<std::int64_t, kMaxLegs> remaining{};
    std::int64_t minimum = 0, quantity_quantum = 1;
    std::int64_t earliest_receive_ns = std::numeric_limits<std::int64_t>::max();
    std::int64_t latest_receive_ns = 0;
    for (std::size_t i = 0; i < relation.leg_count; ++i) {
        const auto& leg = relation.legs[i];
        if (!leg.coefficient.valid() || leg.book_handle >= books.size() || !valid_book(books[leg.book_handle], buy)) {
            out.reject = leg.book_handle >= books.size() ? HotReject::MissingBook : HotReject::IncompleteDepth;
            return out;
        }
        if (!valid_fee_terms(leg)) {
            out.reject = HotReject::UnknownFee; return out;
        }
        for (std::size_t j = 0; j < i; ++j) if (relation.legs[j].book_handle == leg.book_handle) return out;
        if (!buy) {
            if (leg.book_handle >= resources.inventory.size() || resources.inventory[leg.book_handle] <= 0) {
                out.reject = HotReject::InventoryUnavailable; return out;
            }
            const __int128 bound = static_cast<__int128>(resources.inventory[leg.book_handle]) * leg.coefficient.denominator / leg.coefficient.numerator;
            capacity = std::min(capacity, static_cast<std::int64_t>(std::min<__int128>(bound, capacity)));
        }
        const auto receive_ns = books[leg.book_handle].receive_monotonic_ns;
        if (timing.now_receive_monotonic_ns > 0 && timing.maximum_book_age_ns > 0
            && (receive_ns <= 0 || receive_ns > timing.now_receive_monotonic_ns
                || timing.now_receive_monotonic_ns - receive_ns > timing.maximum_book_age_ns)) {
            out.reject = HotReject::StaleBook;
            return out;
        }
        if (receive_ns > 0) {
            earliest_receive_ns = std::min(earliest_receive_ns, receive_ns);
            latest_receive_ns = std::max(latest_receive_ns, receive_ns);
        }
        // ceil(minimum leg shares / coefficient) in relation units.
        const __int128 scaled = (static_cast<__int128>(std::max<std::int64_t>(0, leg.minimum_order_microunits))
            * leg.coefficient.denominator + leg.coefficient.numerator - 1) / leg.coefficient.numerator;
        if (scaled > std::numeric_limits<std::int64_t>::max()) { out.reject = HotReject::NumericOverflow; return out; }
        minimum = std::max(minimum, static_cast<std::int64_t>(scaled));
        const auto divisor = leg.coefficient.denominator / std::gcd(leg.coefficient.numerator, leg.coefficient.denominator);
        const auto gcd = std::gcd(quantity_quantum, divisor);
        if (quantity_quantum > std::numeric_limits<std::int64_t>::max() / (divisor / gcd)) return out;
        quantity_quantum *= divisor / gcd;
    }
    if (timing.maximum_leg_skew_ns > 0 && latest_receive_ns > 0
        && earliest_receive_ns != std::numeric_limits<std::int64_t>::max()
        && latest_receive_ns - earliest_receive_ns > timing.maximum_leg_skew_ns) {
        out.reject = HotReject::LegSkew;
        return out;
    }
    const __int128 rounded_minimum = ((static_cast<__int128>(minimum) + quantity_quantum - 1) / quantity_quantum) * quantity_quantum;
    if (rounded_minimum > std::numeric_limits<std::int64_t>::max()) { out.reject = HotReject::NumericOverflow; return out; }
    minimum = static_cast<std::int64_t>(rounded_minimum);

    // Canonical binary specialization shares the frozen economic function.
    // Identity, depth, fee, timing and inventory guards above still apply.
    if (relation.leg_count == 2 && relation.guaranteed_payout_microunits == kShareMicrounits
        && relation.legs[0].coefficient.numerator == relation.legs[0].coefficient.denominator
        && relation.legs[1].coefficient.numerator == relation.legs[1].coefficient.denominator
        && relation.legs[0].fee_rate == relation.legs[1].fee_rate
        && relation.legs[0].fee_exponent == relation.legs[1].fee_exponent
        && relation.legs[0].fee_rate == 0.0) {
        const auto c = pure_arb::sweep(books[relation.legs[0].book_handle], books[relation.legs[1].book_handle],
            relation.legs[0].fee_rate, relation.legs[0].fee_exponent,
            static_cast<double>(relation.reserve_per_unit_microunits) / kShareMicrounits, buy, capacity);
        const auto notionals = c.yes_notional + c.no_notional;
        const auto raw = buy ? c.shares() - notionals : notionals - c.shares();
        const auto paid_fees = raw - c.gross_locked_pnl;
        const auto used_capital = (buy ? notionals + paid_fees : 0.0)
            + c.shares() * relation.reserve_per_unit_microunits / kShareMicrounits;
        // Tight budgets need the generic partial-breakpoint capital search.
        if (used_capital * kShareMicrounits <= resources.capital_microunits
            && raw < 9e12 && used_capital < 9e12) {
            if (c.shares_microunits <= 0) { out.reject = HotReject::NoPositiveEdge; return out; }
            if (c.shares_microunits < minimum) { out.reject = HotReject::MinimumOrder; return out; }
            out.quantity_microunits = c.shares_microunits;
            out.gross_pnl_microunits = std::llround(c.gross_locked_pnl*kShareMicrounits);
            out.net_pnl_microunits = std::llround(c.conservative_locked_pnl*kShareMicrounits);
            out.raw_pnl_microunits = std::llround(raw*kShareMicrounits);
            out.fees_microunits = std::llround(paid_fees*kShareMicrounits);
            out.capital_required_microunits = static_cast<std::int64_t>(std::ceil(used_capital*kShareMicrounits));
            out.levels_consumed[0] = c.yes_levels_used; out.levels_consumed[1] = c.no_levels_used;
            out.levels_used = c.yes_levels_used + c.no_levels_used;
            out.reject = out.net_pnl_microunits > 0 ? HotReject::Accepted : HotReject::NoPositiveEdge;
            return out;
        }
    }

    std::int64_t quantity = 0;
    double gross = 0.0, net = 0.0, raw_pnl = 0.0, fees = 0.0, capital = 0.0;
    for (;;) {
        std::int64_t step_relation_units = capacity - quantity;
        for (std::size_t i = 0; i < relation.leg_count; ++i) {
            const auto& leg = relation.legs[i]; const auto& book = books[leg.book_handle];
            const auto count = buy ? book.ask_level_count : book.bid_level_count;
            const auto& levels = buy ? book.ask_levels : book.bid_levels;
            const __int128 minimum_slice = static_cast<__int128>(quantity_quantum)
                * leg.coefficient.numerator / leg.coefficient.denominator;
            if (remaining[i] > 0 && remaining[i] < minimum_slice) {
                remaining[i] = 0; ++level[i];
            }
            while (level[i] < count && remaining[i] <= 0) {
                remaining[i] = levels[level[i]].quantity_microunits;
                if (remaining[i] < minimum_slice) { remaining[i] = 0; ++level[i]; } else break;
            }
            if (level[i] >= count) {
                out.reject = quantity >= minimum ? HotReject::Accepted : HotReject::InsufficientDepth;
                break;
            }
            const __int128 units = static_cast<__int128>(remaining[i]) * leg.coefficient.denominator / leg.coefficient.numerator;
            step_relation_units = static_cast<std::int64_t>(std::min<__int128>(step_relation_units, units));
        }
        step_relation_units -= step_relation_units % quantity_quantum;
        if (out.reject == HotReject::Accepted || out.reject == HotReject::InsufficientDepth || step_relation_units <= 0) break;
        auto cashflow = [&](std::int64_t units) {
          std::array<double, 2> result{};
          for (std::size_t i = 0; i < relation.leg_count; ++i) {
            const auto& leg = relation.legs[i]; const auto& book = books[leg.book_handle];
            const auto shares = static_cast<double>(units) * leg.coefficient.value() / kShareMicrounits;
            const auto price = level_price((buy ? book.ask_levels : book.bid_levels)[level[i]]);
            result[0] += shares * price;
            const auto shares_micro = static_cast<std::int64_t>(static_cast<__int128>(units)
                * leg.coefficient.numerator / leg.coefficient.denominator);
            result[1] += exact_fee(shares_micro,
                (buy ? book.ask_levels : book.bid_levels)[level[i]].price_e4, leg);
          }
          return result;
        };
        auto required_capital = [&](std::int64_t units) {
            const auto cf = cashflow(units);
            return (buy ? cf[0] + cf[1] : 0.0) + static_cast<double>(units) * relation.reserve_per_unit_microunits / 1e12;
        };
        const auto available = static_cast<double>(resources.capital_microunits) / kShareMicrounits - capital;
        auto cf = cashflow(step_relation_units);
        if (!std::isfinite(cf[1])) { out.reject = HotReject::NumericOverflow; return out; }
        auto step_capital = (buy ? cf[0] + cf[1] : 0.0)
            + static_cast<double>(step_relation_units) * relation.reserve_per_unit_microunits / 1e12;
        if (step_capital > available) {
            std::int64_t low = 0, high = step_relation_units / quantity_quantum;
            while (low < high) {
                const auto mid = low + (high - low) / 2 + (high - low) % 2;
                if (required_capital(mid * quantity_quantum) <= available) low = mid; else high = mid - 1;
            }
            step_relation_units = low * quantity_quantum;
            if (step_relation_units <= 0) { out.reject = HotReject::CapitalLimit; break; }
            cf = cashflow(step_relation_units);
            step_capital = required_capital(step_relation_units);
        }
        const auto cost = buy ? cf[0] + cf[1] : -cf[0] + cf[1];
        const auto payout = static_cast<double>(step_relation_units) * relation.guaranteed_payout_microunits
            / static_cast<double>(kShareMicrounits * kShareMicrounits);
        const auto reserve = static_cast<double>(step_relation_units) * relation.reserve_per_unit_microunits
            / static_cast<double>(kShareMicrounits * kShareMicrounits);
        const auto gross_step = (buy ? payout : -payout) - cost;
        if (!std::isfinite(gross_step) || !(gross_step - reserve > 1e-12 * step_relation_units / kShareMicrounits)) { out.reject = HotReject::NoPositiveEdge; break; }
        quantity += step_relation_units; gross += gross_step; net += gross_step - reserve;
        raw_pnl += gross_step + cf[1]; fees += cf[1]; capital += step_capital;
        for (std::size_t i = 0; i < relation.leg_count; ++i) {
            const auto& leg = relation.legs[i];
            const auto consumed = static_cast<std::int64_t>(static_cast<__int128>(step_relation_units) * leg.coefficient.numerator / leg.coefficient.denominator);
            out.levels_consumed[i] = static_cast<std::uint16_t>(level[i] + 1);
            remaining[i] -= consumed;
            if (remaining[i] <= 0) ++level[i];
        }
    }
    if (quantity <= 0) {
        if (out.reject == HotReject::Accepted || out.reject == HotReject::InvalidRelation) {
            out.reject = HotReject::NoPositiveEdge;
        }
        return out;
    }
    if (quantity < minimum) { out.reject = HotReject::MinimumOrder; return out; }
    const auto monetary_limit = static_cast<double>(std::numeric_limits<std::int64_t>::max()) / kShareMicrounits;
    if (!std::isfinite(net) || net >= monetary_limit || gross >= monetary_limit || raw_pnl >= monetary_limit
        || capital >= monetary_limit || fees >= monetary_limit) { out.reject = HotReject::NumericOverflow; return out; }
    out.reject = HotReject::Accepted; out.quantity_microunits = quantity;
    out.gross_pnl_microunits = static_cast<std::int64_t>(std::llround(gross * kShareMicrounits));
    out.net_pnl_microunits = static_cast<std::int64_t>(std::llround(net * kShareMicrounits));
    out.raw_pnl_microunits = static_cast<std::int64_t>(std::llround(raw_pnl * kShareMicrounits));
    out.fees_microunits = static_cast<std::int64_t>(std::llround(fees * kShareMicrounits));
    out.capital_required_microunits = static_cast<std::int64_t>(std::ceil(capital * kShareMicrounits));
    for (auto count : out.levels_consumed) out.levels_used += count;
    if (out.net_pnl_microunits <= 0) out.reject = HotReject::NoPositiveEdge;
    return out;
}

[[nodiscard]] inline HotDecision evaluate_buy_basket(const CompiledRelation& r, std::span<const BookDeepSnapshot> b,
    HotTimingContext t = {}, HotResources resources = {}) noexcept { return evaluate_basket(r, b, true, t, resources); }
[[nodiscard]] inline HotDecision evaluate_sell_inventory_basket(const CompiledRelation& r, std::span<const BookDeepSnapshot> b,
    HotTimingContext t = {}, HotResources resources = {}) noexcept { return evaluate_basket(r, b, false, t, resources); }
[[nodiscard]] inline HotDecision evaluate_buy(const CompiledRelation& r, std::span<const BookDeepSnapshot> b,
    HotTimingContext t = {}) noexcept { return evaluate_buy_basket(r, b, t); }

template <class Callback>
inline void evaluate_token_update(std::uint32_t token_handle,
                                  std::span<const TokenDependency> dependencies,
                                  std::span<const std::uint32_t> relation_handles,
                                  std::span<const CompiledRelation> relations,
                                  std::span<const BookDeepSnapshot> books,
                                  HotTimingContext timing,
                                  Callback&& callback, HotResources resources = {}) noexcept {
    const auto it = std::lower_bound(dependencies.begin(), dependencies.end(), token_handle,
        [](const TokenDependency& entry, std::uint32_t handle) { return entry.token_handle < handle; });
    if (it == dependencies.end() || it->token_handle != token_handle || it->relation_count > kMaxDependenciesPerToken) return;
    const auto end = static_cast<std::size_t>(it->first_relation) + it->relation_count;
    if (end > relation_handles.size()) return;
    for (std::size_t i = it->first_relation; i < end; ++i) {
        const auto handle = relation_handles[i];
        if (handle < relations.size()) callback(evaluate_basket(relations[handle], books,
            relations[handle].sell_inventory == 0, timing, resources));
    }
}

}  // namespace pm::v7::exact_arb_graph
