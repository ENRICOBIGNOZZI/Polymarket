#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string_view>

namespace pm::v7::clob {

enum class RateLane : std::uint8_t { Order = 0, Cancel = 1 };
enum class RateTier : std::uint8_t {
    Unknown = 0, Standard = 1, Copper = 2, Bronze = 3, Silver = 4,
    Gold = 5, Platinum = 6, Diamond = 7, Elite = 8,
};

struct TierLimits {
    double order_rate_per_second = 40.0;
    double order_burst = 60.0;
    double cancel_rate_per_second = 80.0;
    double cancel_burst = 120.0;
    bool negative_cancel_balance = true;
};

[[nodiscard]] constexpr TierLimits tier_limits(RateTier tier) noexcept {
    switch (tier) {
        case RateTier::Copper: return {60,90,120,180,true};
        case RateTier::Bronze: return {80,120,160,240,true};
        case RateTier::Silver: return {200,300,400,600,true};
        case RateTier::Gold: return {400,600,800,1200,true};
        case RateTier::Platinum: return {450,675,900,1350,false};
        case RateTier::Diamond: return {525,787,1050,1575,false};
        case RateTier::Elite: return {600,900,1200,1800,false};
        case RateTier::Standard:
        case RateTier::Unknown:
        default: return {};
    }
}

[[nodiscard]] inline RateTier parse_rate_tier(std::string_view value) noexcept {
    auto eq=[](std::string_view a,std::string_view b) noexcept {
        if(a.size()!=b.size()) return false;
        for(std::size_t i=0;i<a.size();++i){
            char x=a[i],y=b[i];
            if(x>='a'&&x<='z') x=static_cast<char>(x-'a'+'A');
            if(y>='a'&&y<='z') y=static_cast<char>(y-'a'+'A');
            if(x!=y) return false;
        }
        return true;
    };
    if(eq(value,"STANDARD")) return RateTier::Standard;
    if(eq(value,"COPPER")) return RateTier::Copper;
    if(eq(value,"BRONZE")) return RateTier::Bronze;
    if(eq(value,"SILVER")) return RateTier::Silver;
    if(eq(value,"GOLD")) return RateTier::Gold;
    if(eq(value,"PLATINUM")) return RateTier::Platinum;
    if(eq(value,"DIAMOND")) return RateTier::Diamond;
    if(eq(value,"ELITE")) return RateTier::Elite;
    return RateTier::Unknown;
}

struct RateLimiterSnapshot {
    RateTier tier = RateTier::Standard;
    double order_tokens = 60.0;
    double cancel_tokens = 120.0;
    std::int64_t blocked_until_monotonic_ns = 0;
    std::int64_t venue_reset_unix_seconds = 0;
    std::uint64_t order_tokens_consumed = 0;
    std::uint64_t cancel_tokens_consumed = 0;
    std::uint64_t local_rejections = 0;
    std::uint64_t venue_429s = 0;
    std::uint64_t warning_headers = 0;
};

class ClobRateLimiter final {
public:
    ClobRateLimiter() noexcept = default;

    [[nodiscard]] bool try_acquire(
        RateLane lane, std::int64_t now_ns, std::uint32_t units = 1) noexcept {
        if (units == 0 || now_ns <= 0) return false;
        refill(now_ns);
        if (now_ns < state_.blocked_until_monotonic_ns) {
            ++state_.local_rejections;
            return false;
        }
        const auto cfg=tier_limits(state_.tier);
        double& tokens=lane==RateLane::Cancel?state_.cancel_tokens:state_.order_tokens;
        const double burst=lane==RateLane::Cancel?cfg.cancel_burst:cfg.order_burst;
        if (static_cast<double>(units)>burst+1e-12
            || static_cast<double>(units)>tokens+1e-12) {
            ++state_.local_rejections;
            return false;
        }
        tokens-=static_cast<double>(units);
        if(lane==RateLane::Cancel) state_.cancel_tokens_consumed+=units;
        else state_.order_tokens_consumed+=units;
        return true;
    }

    void observe(
        RateLane lane, double remaining, RateTier tier,
        std::int64_t reset_unix_seconds, bool warning,
        int retry_after_seconds, int http_status,
        std::int64_t now_ns) noexcept {
        refill(now_ns);
        if(tier!=RateTier::Unknown && tier!=state_.tier){
            state_.tier=tier;
            const auto cfg=tier_limits(tier);
            state_.order_tokens=std::min(state_.order_tokens,cfg.order_burst);
            state_.cancel_tokens=std::min(state_.cancel_tokens,cfg.cancel_burst);
        }
        if(std::isfinite(remaining)){
            const auto cfg=tier_limits(state_.tier);
            const double cap=lane==RateLane::Cancel?cfg.cancel_burst:cfg.order_burst;
            double& tokens=lane==RateLane::Cancel?state_.cancel_tokens:state_.order_tokens;
            // Order balances are non-negative. Cancel balances may be negative
            // at the venue, but a local negative balance simply means no cancel
            // token is presently available.
            tokens=std::clamp(remaining,0.0,cap);
        }
        if(reset_unix_seconds>0) state_.venue_reset_unix_seconds=reset_unix_seconds;
        if(warning) ++state_.warning_headers;
        if(http_status==429){
            ++state_.venue_429s;
            if(retry_after_seconds>0 && now_ns>0){
                const auto delay_ns = static_cast<std::int64_t>(retry_after_seconds)
                    * static_cast<std::int64_t>(1'000'000'000);
                const std::int64_t candidate = now_ns + delay_ns;
                state_.blocked_until_monotonic_ns=std::max<std::int64_t>(
                    state_.blocked_until_monotonic_ns,candidate);
            }
        }
    }

    [[nodiscard]] RateLimiterSnapshot snapshot(std::int64_t now_ns) noexcept {
        refill(now_ns);
        return state_;
    }

private:
    void refill(std::int64_t now_ns) noexcept {
        if(now_ns<=0) return;
        if(last_refill_ns_<=0){last_refill_ns_=now_ns;return;}
        if(now_ns<=last_refill_ns_) return;
        const double dt=static_cast<double>(now_ns-last_refill_ns_)/1e9;
        const auto cfg=tier_limits(state_.tier);
        state_.order_tokens=std::min(
            cfg.order_burst,state_.order_tokens+dt*cfg.order_rate_per_second);
        state_.cancel_tokens=std::min(
            cfg.cancel_burst,state_.cancel_tokens+dt*cfg.cancel_rate_per_second);
        last_refill_ns_=now_ns;
    }

    RateLimiterSnapshot state_{};
    std::int64_t last_refill_ns_=0;
};

} // namespace pm::v7::clob
