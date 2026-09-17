#include "pm/v7_clob_rate_limiter.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7::clob {
namespace {
constexpr double kNsPerSecond = 1'000'000'000.0;
}

ClobRateLimiter::ClobRateLimiter(ClobRateLimitPolicy policy) noexcept {
    signer_order_.policy = policy.signer_order;
    signer_cancel_.policy = policy.signer_cancel;
    cloudflare_order_.policy = policy.cloudflare_order;
    cloudflare_cancel_.policy = policy.cloudflare_cancel;
    signer_order_.tokens = std::max(0.0, signer_order_.policy.burst_tokens);
    signer_cancel_.tokens = std::max(0.0, signer_cancel_.policy.burst_tokens);
    cloudflare_order_.tokens = std::max(0.0, cloudflare_order_.policy.burst_tokens);
    cloudflare_cancel_.tokens = std::max(0.0, cloudflare_cancel_.policy.burst_tokens);
}

void ClobRateLimiter::refill(Bucket& bucket, std::int64_t now_ns) noexcept {
    if (now_ns <= 0 || bucket.policy.rate_per_second <= 0.0
        || bucket.policy.burst_tokens <= 0.0) return;
    if (bucket.last_refill_ns == 0) {
        bucket.last_refill_ns = now_ns;
        return;
    }
    if (now_ns <= bucket.last_refill_ns) return;
    const double elapsed = static_cast<double>(now_ns - bucket.last_refill_ns) / kNsPerSecond;
    bucket.tokens = std::min(bucket.policy.burst_tokens,
                             bucket.tokens + elapsed * bucket.policy.rate_per_second);
    bucket.last_refill_ns = now_ns;
}

std::int64_t ClobRateLimiter::wait_until(const Bucket& bucket,
                                         double cost,
                                         std::int64_t now_ns) noexcept {
    if (cost <= bucket.tokens) return now_ns;
    if (bucket.policy.rate_per_second <= 0.0)
        return std::numeric_limits<std::int64_t>::max();
    const double seconds = (cost - bucket.tokens) / bucket.policy.rate_per_second;
    const auto wait_ns = static_cast<std::int64_t>(std::ceil(seconds * kNsPerSecond));
    if (wait_ns < 0 || now_ns > std::numeric_limits<std::int64_t>::max() - wait_ns)
        return std::numeric_limits<std::int64_t>::max();
    return now_ns + wait_ns;
}

ClobRateLimiter::Bucket& ClobRateLimiter::signer(RequestClass request_class) noexcept {
    return request_class == RequestClass::Cancel ? signer_cancel_ : signer_order_;
}

ClobRateLimiter::Bucket& ClobRateLimiter::cloudflare(RequestClass request_class) noexcept {
    return request_class == RequestClass::Cancel ? cloudflare_cancel_ : cloudflare_order_;
}

RateAdmission ClobRateLimiter::admit(RequestClass request_class,
                                     double token_cost,
                                     std::int64_t now_ns) noexcept {
    RateAdmission out;
    if (!(token_cost > 0.0) || !std::isfinite(token_cost) || now_ns <= 0) return out;
    auto& signer_bucket = signer(request_class);
    auto& cf_bucket = cloudflare(request_class);
    refill(signer_bucket, now_ns);
    refill(cf_bucket, now_ns);
    out.next_admissible_ns = std::max(wait_until(signer_bucket, token_cost, now_ns),
                                      wait_until(cf_bucket, token_cost, now_ns));
    if (out.next_admissible_ns != now_ns) return out;
    signer_bucket.tokens -= token_cost;
    cf_bucket.tokens -= token_cost;
    out.admitted = 1;
    return out;
}

std::int64_t ClobRateLimiter::next_admissible_ns(RequestClass request_class,
                                                 double token_cost,
                                                 std::int64_t now_ns) noexcept {
    if (!(token_cost > 0.0) || !std::isfinite(token_cost) || now_ns <= 0) return 0;
    auto& signer_bucket = signer(request_class);
    auto& cf_bucket = cloudflare(request_class);
    refill(signer_bucket, now_ns);
    refill(cf_bucket, now_ns);
    return std::max(wait_until(signer_bucket, token_cost, now_ns),
                    wait_until(cf_bucket, token_cost, now_ns));
}

void ClobRateLimiter::clamp_signer_remaining(RequestClass request_class,
                                             double remote_remaining,
                                             std::int64_t now_ns) noexcept {
    if (!std::isfinite(remote_remaining) || now_ns <= 0) return;
    auto& bucket = signer(request_class);
    refill(bucket, now_ns);
    bucket.tokens = std::min(bucket.tokens, std::max(0.0, remote_remaining));
}

} // namespace pm::v7::clob
