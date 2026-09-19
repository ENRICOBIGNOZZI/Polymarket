#pragma once
#include "pm/v7_crypto_decision_lane.hpp"
#include <array>
#include <string>
#include <string_view>

namespace pm::v7 {
inline constexpr std::size_t kProbabilityFeatures=18;
inline constexpr std::array<std::string_view,kProbabilityFeatures> kProbabilityFeatureNames{
    "intercept","market_logit","absolute_shock_scaled","confirmation_scaled",
    "log_signal_age","log_tte","spread_ticks","selected_imbalance",
    "asset_BTC","asset_ETH","asset_SOL","asset_XRP","asset_DOGE","asset_BNB",
    "horizon_M15","horizon_H1","horizon_H4","horizon_D1"};
inline constexpr std::array<std::string_view,6> kProbabilityAssets{"BTC","ETH","SOL","XRP","DOGE","BNB"};
inline constexpr std::array<std::string_view,5> kProbabilityHorizons{"M5","M15","H1","H4","D1"};
struct NativeProbabilityModel {
    std::array<double,kProbabilityFeatures> coefficients{};
    std::array<std::array<double,kProbabilityFeatures>,kProbabilityFeatures> covariance{};
    std::array<double,6> shock_scales{};
    double uncertainty_z=1.645, explicit_logit_reserve=.25;
    double execution_reserve_per_share=.005;
    std::int64_t maximum_order_cost_microdollars=3'750'000;
    std::int64_t maximum_quantity_microunits=20'000'000;
    std::uint64_t version=0;
    bool loaded=false;
    std::string artifact_sha256;
    // IO, JSON and hashing happen once on the cold startup path.
    [[nodiscard]] static NativeProbabilityModel load(
        const std::string& path, std::string_view expected_code_sha);
    // Inference is bounded, allocation-free and uses only the input causal cut.
    [[nodiscard]] SettlementProbabilityForecast predict(
        const NativeCryptoDecisionInput& input, std::string_view asset,
        std::string_view horizon) const noexcept;
};
[[nodiscard]] bool probability_features(const NativeCryptoDecisionInput& input,
    std::string_view asset, std::string_view horizon, const std::array<double,6>& scales,
    std::array<double,kProbabilityFeatures>& out) noexcept;
} // namespace pm::v7
