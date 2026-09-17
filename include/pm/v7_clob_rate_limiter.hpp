#pragma once

#include <cstdint>

namespace pm::v7::clob {

enum class RequestClass : std::uint8_t { Order = 1, Cancel = 2 };

struct BucketPolicy {
    double rate_per_second = 0.0;
    double burst_tokens = 0.0;
};

struct ClobRateLimitPolicy {
    // Deliberate margin below the current Standard per-signer buckets.
    BucketPolicy signer_order{36.0, 54.0};
    BucketPolicy signer_cancel{72.0, 108.0};
    // Secondary local guard below Cloudflare's sustained order/cancel limits.
    BucketPolicy cloudflare_order{180.0, 4'500.0};
    BucketPolicy cloudflare_cancel{180.0, 4'500.0};
};

struct RateAdmission {
    std::int64_t next_admissible_ns = 0;
    std::uint8_t admitted = 0;
};

class ClobRateLimiter final {
public:
    explicit ClobRateLimiter(ClobRateLimitPolicy policy = {}) noexcept;

    [[nodiscard]] RateAdmission admit(RequestClass request_class,
                                      double token_cost,
                                      std::int64_t now_ns) noexcept;
    [[nodiscard]] std::int64_t next_admissible_ns(RequestClass request_class,
                                                  double token_cost,
                                                  std::int64_t now_ns) noexcept;
    void clamp_signer_remaining(RequestClass request_class,
                                double remote_remaining,
                                std::int64_t now_ns) noexcept;

private:
    struct Bucket {
        BucketPolicy policy{};
        double tokens = 0.0;
        std::int64_t last_refill_ns = 0;
    };

    static void refill(Bucket& bucket, std::int64_t now_ns) noexcept;
    static std::int64_t wait_until(const Bucket& bucket,
                                   double cost,
                                   std::int64_t now_ns) noexcept;
    Bucket& signer(RequestClass request_class) noexcept;
    Bucket& cloudflare(RequestClass request_class) noexcept;

    Bucket signer_order_{};
    Bucket signer_cancel_{};
    Bucket cloudflare_order_{};
    Bucket cloudflare_cancel_{};
};

} // namespace pm::v7::clob
