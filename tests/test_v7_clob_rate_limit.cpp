#include "pm/v7_clob_http1_response.hpp"
#include "pm/v7_clob_rate_limit.hpp"

#include <cassert>
#include <cmath>
#include <cstring>
#include <string_view>

int main() {
    using namespace pm::v7;
    {
        clob::ClobRateLimiter limiter;
        const std::int64_t t0=1'000'000'000LL;
        for(int i=0;i<60;++i) assert(limiter.try_acquire(clob::RateLane::Order,t0,1));
        assert(!limiter.try_acquire(clob::RateLane::Order,t0,1));
        assert(limiter.try_acquire(clob::RateLane::Order,t0+25'000'000LL,1)); // 40/s
        limiter.observe(clob::RateLane::Order,599.0,clob::RateTier::Gold,
                        0,false,0,200,t0+25'000'000LL);
        auto snap=limiter.snapshot(t0+25'000'000LL);
        assert(snap.tier==clob::RateTier::Gold);
        assert(std::abs(snap.order_tokens-599.0)<1e-12);
        assert(limiter.try_acquire(clob::RateLane::Order,t0+25'000'000LL,15));
        assert(!limiter.try_acquire(clob::RateLane::Order,t0+25'000'000LL,601));
        limiter.observe(clob::RateLane::Order,0.0,clob::RateTier::Gold,
                        0,true,2,429,t0+25'000'000LL);
        assert(!limiter.try_acquire(clob::RateLane::Order,t0+1'000'000'000LL,1));
        assert(limiter.try_acquire(clob::RateLane::Order,t0+2'100'000'000LL,1));
        snap=limiter.snapshot(t0+2'100'000'000LL);
        assert(snap.venue_429s==1);
        assert(snap.warning_headers==1);
    }
    {
        clob_transport::FixedHttp1Response parser;
        constexpr std::string_view raw=
            "HTTP/1.1 429 Too Many Requests\r\n"
            "Content-Length: 2\r\n"
            "Retry-After: 2\r\n"
            "Poly-RateLimit-Remaining: 17.5\r\n"
            "Poly-RateLimit-Reset: 1780000123\r\n"
            "Poly-RateLimit-Tier: GOLD\r\n"
            "Poly-RateLimit-Warning: true\r\n"
            "\r\n{}";
        auto dst=parser.writable();
        assert(dst.size()>=raw.size());
        std::memcpy(dst.data(),raw.data(),raw.size());
        assert(parser.commit(raw.size())==clob_transport::Http1ResponseState::Complete);
        assert(parser.status_code()==429);
        assert(parser.retry_after_seconds()==2);
        assert(std::abs(parser.rate_limit_remaining()-17.5)<1e-12);
        assert(parser.rate_limit_reset_unix_seconds()==1'780'000'123LL);
        assert(parser.rate_limit_tier()=="GOLD");
        assert(parser.rate_limit_warning());
    }
    return 0;
}
