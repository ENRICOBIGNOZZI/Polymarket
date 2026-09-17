#include "pm/v7_clob_eip712.hpp"
#include "pm/v7_clob_http_frame.hpp"
#include "pm/v7_clob_order_amounts.hpp"
#include "pm/v7_clob_order_salt.hpp"
#include "pm/v7_clob_wire.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string_view>
#include <vector>

using namespace pm::v7;
using namespace pm::v7::clob_eip712;
using namespace pm::v7::clob_http_frame;
using namespace pm::v7::clob_order;
using namespace pm::v7::clob_wire;

template <std::size_t N>
std::string_view decimal(std::uint64_t value, std::array<char, N>& buffer) noexcept {
    const auto [end, error] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
    return error == std::errc{} ? std::string_view(buffer.data(), end - buffer.data()) : std::string_view{};
}
struct Stats {
    std::vector<std::int64_t> samples;
    void add(std::int64_t value) { samples.push_back(value); }
    void print(std::string_view name) {
        std::sort(samples.begin(), samples.end());
        const auto quantile = [&](double p) {
            const auto index = std::min(samples.size() - 1,
                static_cast<std::size_t>(p * static_cast<double>(samples.size())));
            return samples[index];
        };
        std::cout << name << " p50_ns=" << quantile(0.50)
                  << " p95_ns=" << quantile(0.95)
                  << " p99_ns=" << quantile(0.99) << '\n';
    }
};

std::int64_t elapsed_ns(std::chrono::steady_clock::time_point start,
                        std::chrono::steady_clock::time_point end) noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
}

int main(int argc, char** argv) {
    const std::size_t sample_count = argc > 1
        ? static_cast<std::size_t>(std::strtoull(argv[1], nullptr, 10))
        : 100'000U;
    if (sample_count == 0) return 1;
    constexpr std::string_view maker = "0x1111111111111111111111111111111111111111";
    constexpr std::string_view zeros =
        "0x0000000000000000000000000000000000000000000000000000000000000000";
    constexpr std::string_view token = "1234";
    constexpr std::string_view dummy_order_signature =
        "0x1111111111111111111111111111111111111111111111111111111111111111"
        "22222222222222222222222222222222222222222222222222222222222222221b";

    ExchangeV2DomainView domain{80002, "0xE111180000d2663C0091e4f400237545B87B996B"};
    ExchangeV2PreparedStaticView fixed{maker, maker, token, 0, 3, zeros, zeros};
    ExchangeV2PreparedOrderHasher hasher(domain, fixed);
    if (!hasher.valid()) return 2;

    L2HmacSigner hmac("AAECAwQFBgcICQoLDA0ODw==");
    if (!hmac.valid()) return 3;
    OrderSaltSequence salts(123456789ULL);
    if (!salts.valid()) return 4;

    Stats amount, eip712, json_body, l2_hmac, http_frame, total;
    amount.samples.reserve(sample_count); eip712.samples.reserve(sample_count);
    json_body.samples.reserve(sample_count); l2_hmac.samples.reserve(sample_count);
    http_frame.samples.reserve(sample_count); total.samples.reserve(sample_count);
    std::uint64_t sink = 0;
    for (std::size_t i = 0; i < 3'000; ++i) {
        const auto amounts = marketable_limit_amounts(Side::Buy, 6500, 100, 2'500'000);
        Hash32 digest{};
        const auto salt = salts.next();
        if (!hasher.digest_u64(salt, static_cast<std::uint64_t>(amounts.maker_amount),
                               static_cast<std::uint64_t>(amounts.taker_amount),
                               1'710'000'000'000ULL + i, digest)) return 5;
        sink += digest[0];
    }

    for (std::size_t i = 0; i < sample_count; ++i) {
        const auto t0 = std::chrono::steady_clock::now();
        const auto amounts = marketable_limit_amounts(
            Side::Buy, 6500, 100, 2'500'000 + static_cast<std::int64_t>(i % 100U) * 10'000);
        if (!amounts.valid) return 6;
        const auto salt = salts.next();
        std::array<char, 32> salt_buf{}, maker_buf{}, taker_buf{}, ts_buf{}, l2_ts_buf{};
        const auto salt_text = decimal(salt, salt_buf);
        const auto maker_text = decimal(static_cast<std::uint64_t>(amounts.maker_amount), maker_buf);
        const auto taker_text = decimal(static_cast<std::uint64_t>(amounts.taker_amount), taker_buf);
        const auto timestamp = 1'710'000'000'000ULL + i;
        const auto timestamp_text = decimal(timestamp, ts_buf);
        const auto l2_timestamp_text = decimal(timestamp / 1'000ULL, l2_ts_buf);
        const auto t1 = std::chrono::steady_clock::now();
        Hash32 digest{};
        if (!hasher.digest_u64(salt, static_cast<std::uint64_t>(amounts.maker_amount),
                               static_cast<std::uint64_t>(amounts.taker_amount),
                               timestamp, digest)) return 7;
        const auto t2 = std::chrono::steady_clock::now();

        std::array<char, 2048> body{};
        SignedMarketOrderView order{zeros, "0", maker, maker_text, zeros, salt_text,
                                    "BUY", dummy_order_signature, 3, maker, taker_text,
                                    timestamp_text, token};
        PostMarketOrderView post{order, maker, MarketOrderType::FAK};
        const auto body_size = serialize_post_market_order(post, body);
        if (body_size == 0) return 8;
        const auto t3 = std::chrono::steady_clock::now();

        std::array<char, 128> l2_signature{};
        const auto l2_size = hmac.sign(l2_timestamp_text,
            std::string_view(body.data(), body_size), l2_signature);
        if (l2_size == 0) return 9;
        const auto t4 = std::chrono::steady_clock::now();

        L2AuthHeadersView auth{maker, std::string_view(l2_signature.data(), l2_size),
                               l2_timestamp_text, "api-key-test", "pass-test"};
        std::array<char, 4096> frame{};
        const auto frame_size = serialize_post_order_http1(auth,
            std::string_view(body.data(), body_size), frame);
        if (frame_size == 0) return 10;
        const auto t5 = std::chrono::steady_clock::now();
        amount.add(elapsed_ns(t0, t1));
        eip712.add(elapsed_ns(t1, t2));
        json_body.add(elapsed_ns(t2, t3));
        l2_hmac.add(elapsed_ns(t3, t4));
        http_frame.add(elapsed_ns(t4, t5));
        total.add(elapsed_ns(t0, t5));
        sink += digest[0] + static_cast<unsigned char>(frame[frame_size - 1]);
    }

    amount.print("amount_salt_decimal");
    eip712.print("eip712");
    json_body.print("json_body");
    l2_hmac.print("l2_hmac");
    http_frame.print("http_frame");
    total.print("total_excluding_secp256k1_and_socket");
    std::cout << "samples=" << sample_count << " sink=" << sink << '\n';
    return 0;
}
