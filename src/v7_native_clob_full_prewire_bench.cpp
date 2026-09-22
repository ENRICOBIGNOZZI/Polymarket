#include "pm/v7_clob_order_amounts.hpp"
#include "pm/v7_clob_order_salt.hpp"
#include "pm/v7_clob_prepared_post.hpp"
#include "pm/v7_clob_rate_limit.hpp"
#include "pm/v7_poly1271.hpp"

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
namespace {
using Clock = std::chrono::steady_clock;
using Ns = std::chrono::nanoseconds;

[[nodiscard]] std::int64_t ns_now() noexcept {
    return std::chrono::duration_cast<Ns>(
        Clock::now().time_since_epoch()).count();
}

template <typename T, std::size_t N>
[[nodiscard]] std::string_view dec(
    T value, std::array<char, N>& buffer) noexcept {
    const auto r = std::to_chars(
        buffer.data(), buffer.data() + buffer.size(), value);
    return r.ec == std::errc{}
        ? std::string_view(buffer.data(), r.ptr - buffer.data())
        : std::string_view{};
}

[[nodiscard]] std::int64_t quantile(
    std::vector<std::int64_t>& values, double p) {
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>(
        static_cast<double>(values.size() - 1) * p);
    return values[index];
}

void print_dist(const char* name, std::vector<std::int64_t> values) {
    const auto p50 = quantile(values, .50);
    const auto p95 = quantile(values, .95);
    const auto p99 = quantile(values, .99);
    const auto p999 = quantile(values, .999);
    std::cout << '"' << name << "\":{\"p50\":" << p50
              << ",\"p95\":" << p95
              << ",\"p99\":" << p99
              << ",\"p99_9\":" << p999
              << ",\"max\":" << values.back() << '}';
}
} // namespace

int main(int argc, char** argv) {
    const std::size_t samples = argc > 1
        ? static_cast<std::size_t>(std::strtoull(argv[1], nullptr, 10))
        : 200'000;
    if (samples < 16) return 64;

    constexpr std::string_view deposit =
        "0x1111111111111111111111111111111111111111";
    constexpr std::string_view signer_address =
        "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf";
    constexpr std::string_view exchange =
        "0xE111180000d2663C0091e4f400237545B87B996B";
    constexpr std::string_view metadata =
        "0x0000000000000000000000000000000000000000000000000000000000000000";

    std::array<std::uint8_t, 32> key{};
    key.back() = 1;

    clob_eip712::ExchangeV2PreparedOrderHasher order_hasher(
        {137, exchange},
        {deposit, deposit, "1234", 0, 3, metadata, metadata});
    poly1271::PreparedHasher poly_hasher(
        137, deposit, order_hasher.domain_separator());
    poly1271::Secp256k1Signer signer(key);
    clob_order::OrderSaltSequence salt(123);
    clob_post::PreparedPostOrderBuilder post(
        {metadata, "0", deposit, metadata, "BUY", 3, deposit, "1234",
         "00000000-0000-0000-0000-000000000000",
         clob_wire::MarketOrderType::FAK},
        signer_address, "00000000-0000-0000-0000-000000000000",
        "pass-test", "YWJj");
    clob::ClobRateLimiter limiter;

    if (!order_hasher.valid() || !poly_hasher.valid()
        || !signer.valid() || !salt.valid() || !post.valid()) {
        return 65;
    }

    std::vector<std::int64_t> limiter_ns;
    std::vector<std::int64_t> amount_ns;
    std::vector<std::int64_t> sign_ns;
    std::vector<std::int64_t> dynamic_fields_ns;
    std::vector<std::int64_t> order_struct_hash_ns;
    std::vector<std::int64_t> poly_digest_ns;
    std::vector<std::int64_t> secp_sign_ns;
    std::vector<std::int64_t> signature_wrap_ns;
    std::vector<std::int64_t> frame_ns;
    std::vector<std::int64_t> total_ns;
    limiter_ns.reserve(samples);
    amount_ns.reserve(samples);
    sign_ns.reserve(samples);
    dynamic_fields_ns.reserve(samples);
    order_struct_hash_ns.reserve(samples);
    poly_digest_ns.reserve(samples);
    secp_sign_ns.reserve(samples);
    signature_wrap_ns.reserve(samples);
    frame_ns.reserve(samples);
    total_ns.reserve(samples);

    // Warm the amount/signing path. The rate limiter is deliberately not
    // warmed here so the measured state starts from a full Standard bucket.
    for (std::size_t i = 0; i < 4'000; ++i) {
        const auto a = clob_order::marketable_limit_amounts(
            Side::Buy, 5000, 100, 5'000'000);
        if (!a.valid) return 66;
        std::array<char, poly1271::kWrappedSignatureHexChars> signature{};
        if (!poly1271::sign_prepared_poly1271_hex(
                order_hasher, poly_hasher, signer, 123 + i,
                a.maker_amount, a.taker_amount,
                1'710'000'000'000ULL + i, signature)) {
            return 67;
        }
    }

    // Prove the decomposed benchmark is byte-identical to the production
    // composite helper before timing it.
    {
        const auto a = clob_order::marketable_limit_amounts(
            Side::Buy, 5000, 100, 5'000'000);
        if (!a.valid) return 76;
        constexpr std::uint64_t parity_salt = 9'999;
        constexpr std::uint64_t parity_ts = 1'710'123'456'789ULL;
        std::array<char, poly1271::kWrappedSignatureHexChars> composite{};
        std::array<char, poly1271::kWrappedSignatureHexChars> decomposed{};
        if (!poly1271::sign_prepared_poly1271_hex(
                order_hasher, poly_hasher, signer, parity_salt,
                a.maker_amount, a.taker_amount, parity_ts, composite)) {
            return 77;
        }
        clob_eip712::Hash32 contents{}, digest{};
        std::array<std::uint8_t, poly1271::kEvmSignatureBytes> inner{};
        if (!order_hasher.struct_hash_u64(
                parity_salt, a.maker_amount, a.taker_amount, parity_ts, contents)
            || !poly_hasher.digest(contents, digest)
            || !signer.sign_digest(digest, inner)
            || !poly1271::wrap_signature_hex(
                inner, order_hasher.domain_separator(), contents, decomposed)
            || composite != decomposed) {
            return 78;
        }
    }

    constexpr std::int64_t limiter_origin_ns = 1'000'000'000LL;
    constexpr std::int64_t limiter_step_ns = 25'000'000LL; // Standard refill = 1 token.

    for (std::size_t i = 0; i < samples; ++i) {
        const auto t0 = ns_now();

        const auto rl0 = ns_now();
        if (!limiter.try_acquire(
                clob::RateLane::Order,
                limiter_origin_ns + static_cast<std::int64_t>(i) * limiter_step_ns,
                1)) {
            return 68;
        }
        const auto rl1 = ns_now();

        const auto amounts = clob_order::marketable_limit_amounts(
            Side::Buy, 5000, 100, 5'000'000);
        if (!amounts.valid) return 69;
        const auto t1 = ns_now();

        const auto salt_value = salt.next();
        const std::uint64_t timestamp_ms = 1'710'000'000'000ULL + i;
        std::array<char, 32> salt_buf{}, maker_buf{}, taker_buf{}, ts_buf{};
        std::array<char, 24> req_ts_buf{};
        const auto salt_sv = dec(salt_value, salt_buf);
        const auto maker_sv = dec(amounts.maker_amount, maker_buf);
        const auto taker_sv = dec(amounts.taker_amount, taker_buf);
        const auto ts_sv = dec(timestamp_ms, ts_buf);
        const auto req_ts_sv = dec(timestamp_ms / 1000U, req_ts_buf);
        if (salt_sv.empty() || maker_sv.empty() || taker_sv.empty()
            || ts_sv.empty() || req_ts_sv.empty()) {
            return 70;
        }

        std::array<char, poly1271::kWrappedSignatureHexChars> signature{};
        if (!poly1271::sign_prepared_poly1271_hex(
                order_hasher, poly_hasher, signer, salt_value,
                amounts.maker_amount, amounts.taker_amount,
                timestamp_ms, signature)) {
            return 71;
        }
        const auto t2 = ns_now();

        clob_wire::MarketOrderDynamicView dynamic{};
        dynamic.maker_amount = maker_sv;
        dynamic.salt_decimal = salt_sv;
        dynamic.signature = {signature.data(), signature.size()};
        dynamic.taker_amount = taker_sv;
        dynamic.timestamp_ms = ts_sv;
        std::array<char, 8192> frame{};
        if (post.build(dynamic, req_ts_sv, frame) == 0) return 72;
        const auto t3 = ns_now();

        limiter_ns.push_back(rl1 - rl0);
        amount_ns.push_back(t1 - rl1);
        sign_ns.push_back(t2 - t1);
        frame_ns.push_back(t3 - t2);
        total_ns.push_back(t3 - t0);
    }

    // Profile signing substages separately. The production latency gate above
    // remains byte-for-byte on sign_prepared_poly1271_hex.
    for (std::size_t i = 0; i < samples; ++i) {
        const auto amounts = clob_order::marketable_limit_amounts(
            Side::Buy, 5000, 100, 5'000'000);
        if (!amounts.valid) return 79;
        const std::uint64_t salt_value = 1'000'000ULL + i;
        const std::uint64_t timestamp_ms = 1'720'000'000'000ULL + i;

        const auto f0 = ns_now();
        std::array<char, 32> salt_buf{}, maker_buf{}, taker_buf{}, ts_buf{};
        std::array<char, 24> req_ts_buf{};
        const auto salt_sv = dec(salt_value, salt_buf);
        const auto maker_sv = dec(amounts.maker_amount, maker_buf);
        const auto taker_sv = dec(amounts.taker_amount, taker_buf);
        const auto ts_sv = dec(timestamp_ms, ts_buf);
        const auto req_ts_sv = dec(timestamp_ms / 1000U, req_ts_buf);
        if (salt_sv.empty() || maker_sv.empty() || taker_sv.empty()
            || ts_sv.empty() || req_ts_sv.empty()) {
            return 80;
        }
        const auto f1 = ns_now();

        clob_eip712::Hash32 contents{}, digest{};
        std::array<std::uint8_t, poly1271::kEvmSignatureBytes> inner{};
        std::array<char, poly1271::kWrappedSignatureHexChars> wrapped{};
        if (!order_hasher.struct_hash_u64(
                salt_value, amounts.maker_amount, amounts.taker_amount,
                timestamp_ms, contents)) {
            return 81;
        }
        const auto f2 = ns_now();
        if (!poly_hasher.digest(contents, digest)) return 82;
        const auto f3 = ns_now();
        if (!signer.sign_digest(digest, inner)) return 83;
        const auto f4 = ns_now();
        if (!poly1271::wrap_signature_hex(
                inner, order_hasher.domain_separator(), contents, wrapped)) {
            return 84;
        }
        const auto f5 = ns_now();

        dynamic_fields_ns.push_back(f1 - f0);
        order_struct_hash_ns.push_back(f2 - f1);
        poly_digest_ns.push_back(f3 - f2);
        secp_sign_ns.push_back(f4 - f3);
        signature_wrap_ns.push_back(f5 - f4);
    }

    std::cout
        << "{\"schema\":\"polymarket_v7_native_clob_full_prewire_bench_v2\","
        << "\"paper_only\":true,\"authenticated_execution\":false,"
        << "\"real_order_submission\":false,\"production_prepared_path\":true,"
        << "\"rate_limiter_included\":true,\"samples\":" << samples
        << ",\"latency_ns\":{";
    print_dist("rate_limit_check", limiter_ns); std::cout << ',';
    print_dist("amounts", amount_ns); std::cout << ',';
    print_dist("prepared_poly1271_sign", sign_ns); std::cout << ',';
    print_dist("dynamic_fields", dynamic_fields_ns); std::cout << ',';
    print_dist("order_struct_hash", order_struct_hash_ns); std::cout << ',';
    print_dist("poly1271_digest", poly_digest_ns); std::cout << ',';
    print_dist("secp256k1_sign", secp_sign_ns); std::cout << ',';
    print_dist("signature_wrap_hex", signature_wrap_ns); std::cout << ',';
    print_dist("zero_copy_post", frame_ns); std::cout << ',';
    print_dist("total", total_ns);
    std::cout << "}}\n";
    return 0;
}
