#pragma once

// Promotion contract for the unified exact-arbitrage graph.  This header is
// deliberately independent from JSON/control-plane code: graph generations
// are compiled off-path into fixed handles before entering this evaluator.

#include "pm/v7_market_state.hpp"

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
    InsufficientDepth,
    MinimumOrder,
    NoPositiveEdge,
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
};

struct CompiledRelation {
    std::uint64_t relation_handle = 0;
    std::uint64_t proof_handle = 0;
    std::int64_t guaranteed_payout_microunits = 0;
    std::int64_t reserve_per_unit_microunits = 0;
    std::uint8_t leg_count = 0;
    std::uint8_t enabled = 0;
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
    std::uint16_t levels_used = 0;
};

[[nodiscard]] inline double level_price(const PriceLevelE4& level) noexcept {
    return static_cast<double>(level.price_e4) / 10'000.0;
}

[[nodiscard]] inline bool valid_book(const BookDeepSnapshot& book) noexcept {
    return book.valid != 0 && book.lineage_continuous != 0 && book.ask_truncated == 0
        && book.ask_level_count > 0 && book.ask_level_count <= book.ask_levels.size();
}

[[nodiscard]] inline double fee_per_share(double price, const CompiledLeg& leg) noexcept {
    if (!std::isfinite(price) || price <= 0.0 || price >= 1.0 || !std::isfinite(leg.fee_rate)
        || leg.fee_rate < 0.0 || leg.fee_rate > 1.0 || !std::isfinite(leg.fee_exponent)
        || leg.fee_exponent < 0.0) return std::numeric_limits<double>::quiet_NaN();
    return leg.fee_rate == 0.0 ? 0.0
        : leg.fee_rate * std::pow(price * (1.0 - price), leg.fee_exponent);
}

// Full-depth N-leg buy sizing.  Every loop advances at least one level, so it
// is bounded by the sum of preallocated book depths, not graph cardinality.
[[nodiscard]] inline HotDecision evaluate_buy(
    const CompiledRelation& relation, std::span<const BookDeepSnapshot> books) noexcept {
    HotDecision out{};
    out.relation_handle = relation.relation_handle;
    if (relation.enabled == 0 || relation.leg_count == 0 || relation.leg_count > kMaxLegs
        || relation.guaranteed_payout_microunits <= 0) return out;

    std::array<std::size_t, kMaxLegs> level{};
    std::array<std::int64_t, kMaxLegs> remaining{};
    std::int64_t minimum = 0, quantity_quantum = 1;
    for (std::size_t i = 0; i < relation.leg_count; ++i) {
        const auto& leg = relation.legs[i];
        if (!leg.coefficient.valid() || leg.book_handle >= books.size() || !valid_book(books[leg.book_handle])) {
            out.reject = leg.book_handle >= books.size() ? HotReject::MissingBook : HotReject::IncompleteDepth;
            return out;
        }
        // ceil(minimum leg shares / coefficient) in relation units.
        const auto scaled = static_cast<long double>(std::max<std::int64_t>(0, leg.minimum_order_microunits))
            * static_cast<long double>(leg.coefficient.denominator) / static_cast<long double>(leg.coefficient.numerator);
        minimum = std::max(minimum, static_cast<std::int64_t>(std::ceil(scaled)));
        const auto divisor = leg.coefficient.denominator;
        const auto gcd = std::gcd(quantity_quantum, divisor);
        if (quantity_quantum > std::numeric_limits<std::int64_t>::max() / (divisor / gcd)) return out;
        quantity_quantum *= divisor / gcd;
    }
    minimum = ((minimum + quantity_quantum - 1) / quantity_quantum) * quantity_quantum;

    std::int64_t quantity = 0;
    double gross = 0.0, net = 0.0;
    for (;;) {
        std::int64_t step_relation_units = std::numeric_limits<std::int64_t>::max();
        for (std::size_t i = 0; i < relation.leg_count; ++i) {
            const auto& leg = relation.legs[i]; const auto& book = books[leg.book_handle];
            while (level[i] < book.ask_level_count && remaining[i] <= 0) {
                remaining[i] = book.ask_levels[level[i]].quantity_microunits;
                if (remaining[i] <= 0) ++level[i]; else break;
            }
            if (level[i] >= book.ask_level_count) {
                out.reject = quantity >= minimum ? HotReject::Accepted : HotReject::InsufficientDepth;
                break;
            }
            const auto units = static_cast<long double>(remaining[i]) * leg.coefficient.denominator / leg.coefficient.numerator;
            step_relation_units = std::min(step_relation_units, static_cast<std::int64_t>(std::floor(units)));
        }
        step_relation_units -= step_relation_units % quantity_quantum;
        if (out.reject == HotReject::Accepted || out.reject == HotReject::InsufficientDepth || step_relation_units <= 0) break;
        double cost = 0.0;
        for (std::size_t i = 0; i < relation.leg_count; ++i) {
            const auto& leg = relation.legs[i]; const auto& book = books[leg.book_handle];
            const auto shares = static_cast<double>(step_relation_units) * leg.coefficient.value() / kShareMicrounits;
            const auto price = level_price(book.ask_levels[level[i]]); const auto fee = fee_per_share(price, leg);
            if (!std::isfinite(fee)) return out;
            cost += shares * (price + fee);
        }
        const auto payout = static_cast<double>(step_relation_units) * relation.guaranteed_payout_microunits
            / static_cast<double>(kShareMicrounits * kShareMicrounits);
        const auto reserve = static_cast<double>(step_relation_units) * relation.reserve_per_unit_microunits
            / static_cast<double>(kShareMicrounits * kShareMicrounits);
        if (!(payout - cost - reserve > 0.0)) { out.reject = HotReject::NoPositiveEdge; break; }
        quantity += step_relation_units; gross += payout - cost; net += payout - cost - reserve;
        for (std::size_t i = 0; i < relation.leg_count; ++i) {
            const auto& leg = relation.legs[i];
            const auto consumed = static_cast<std::int64_t>(std::llround(step_relation_units * leg.coefficient.value()));
            remaining[i] -= consumed;
            if (remaining[i] <= 0) ++level[i];
            ++out.levels_used;
        }
    }
    if (quantity <= 0) {
        if (out.reject == HotReject::Accepted || out.reject == HotReject::InvalidRelation) {
            out.reject = HotReject::NoPositiveEdge;
        }
        return out;
    }
    if (quantity < minimum) { out.reject = HotReject::MinimumOrder; return out; }
    out.reject = HotReject::Accepted; out.quantity_microunits = quantity;
    out.gross_pnl_microunits = static_cast<std::int64_t>(std::llround(gross * kShareMicrounits));
    out.net_pnl_microunits = static_cast<std::int64_t>(std::llround(net * kShareMicrounits));
    return out;
}

template <class Callback>
inline void evaluate_token_update(std::uint32_t token_handle,
                                  std::span<const TokenDependency> dependencies,
                                  std::span<const std::uint32_t> relation_handles,
                                  std::span<const CompiledRelation> relations,
                                  std::span<const BookDeepSnapshot> books,
                                  Callback&& callback) noexcept {
    const auto it = std::lower_bound(dependencies.begin(), dependencies.end(), token_handle,
        [](const TokenDependency& entry, std::uint32_t handle) { return entry.token_handle < handle; });
    if (it == dependencies.end() || it->token_handle != token_handle || it->relation_count > kMaxDependenciesPerToken) return;
    const auto end = static_cast<std::size_t>(it->first_relation) + it->relation_count;
    if (end > relation_handles.size()) return;
    for (std::size_t i = it->first_relation; i < end; ++i) {
        const auto handle = relation_handles[i];
        if (handle < relations.size()) callback(evaluate_buy(relations[handle], books));
    }
}

}  // namespace pm::v7::exact_arb_graph
