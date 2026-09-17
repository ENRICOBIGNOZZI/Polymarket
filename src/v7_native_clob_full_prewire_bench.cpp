#include "pm/v7_clob_order_amounts.hpp"
#include "pm/v7_clob_order_salt.hpp"
#include "pm/v7_clob_prepared_post.hpp"
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

std::int64_t ns_now() noexcept {
    return std::chrono::duration_cast<Ns>(Clock::now().time_since_epoch()).count();
}

template <typename T, std::size_t N>
std::string_view dec(T value, std::array<char, N>& buffer) noexcept {
    const auto r = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
    return r.ec == std::errc{} ? std::string_view(buffer.data(), r.ptr - buffer.data()) : std::string_view{};
}
std::int64_t quantile(std::vector<std::int64_t>& values, double p) {
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>((values.size() - 1) * p);
    return values[index];
}

void print_dist(const char* name, std::vector<std::int64_t> values) {
    const auto p50 = quantile(values, .50);
    const auto p95 = quantile(values, .95);
    const auto p99 = quantile(values, .99);
    const auto p999 = quantile(values, .999);
    std::cout << '"' << name << "\":{\"p50\":" << p50
              << ",\"p95\":" << p95 << ",\"p99\":" << p99
              << ",\"p99_9\":" << p999 << ",\"max\":" << values.back() << '}';
}

} // namespace

int main(int argc, char** argv) {
    const std::size_t samples = argc > 1
        ? static_cast<std::size_t>(std::strtoull(argv[1], nullptr, 10)) : 200'000;
    if (samples < 16) return 64;

    std::array<std::uint8_t, 32> key{};
    key.back() = 1; // Public test scalar.
    poly1271::Poly1271OrderHasher hasher(
        {80002, "0xE111180000d2663C0091e4f400237545B87B996B"},
        "0x1111111111111111111111111111111111111111");
    poly1271::Secp256k1Signer signer(key);
    clob_order::OrderSaltSequence salt(123);
    clob_post::PreparedPostOrderBuilder post(
        {"0x0000000000000000000000000000000000000000000000000000000000000000",
         "0", "0x1111111111111111111111111111111111111111",
         "0x0000000000000000000000000000000000000000000000000000000000000000",
         "BUY", 3, "0x1111111111111111111111111111111111111111", "1234",
         "00000000-0000-0000-0000-000000000000", clob_wire::MarketOrderType::FAK},
        "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf",
        "00000000-0000-0000-0000-000000000000", "pass-test", "YWJj");
    if (!hasher.valid() || !signer.valid() || !salt.valid() || !post.valid()) return 65;

    std::vector<std::int64_t> amount_ns, sign_ns, frame_ns, total_ns;
    amount_ns.reserve(samples); sign_ns.reserve(samples);
    frame_ns.reserve(samples); total_ns.reserve(samples);

    for (std::size_t i = 0; i < 2'000; ++i) {
        auto a = clob_order::marketable_limit_amounts(Side::Buy, 5000, 100, 5'000'000);
        if (!a.valid) return 66;
    }

    for (std::size_t i = 0; i < samples; ++i) {
        const auto t0 = ns_now();
        const auto amounts = clob_order::marketable_limit_amounts(
            Side::Buy, 5000, 100, 5'000'000);
        if (!amounts.valid) return 67;
        const auto t1 = ns_now();
        std::array<char, 32> salt_buf{}, maker_buf{}, taker_buf{}, ts_buf{};
        std::array<char, 24> req_ts_buf{};
        const auto salt_sv = dec(salt.next(), salt_buf);
        const auto maker_sv = dec(amounts.maker_amount, maker_buf);
        const auto taker_sv = dec(amounts.taker_amount, taker_buf);
        const std::uint64_t timestamp_ms = 1'710'000'000'000ULL + i;
        const auto ts_sv = dec(timestamp_ms, ts_buf);
        const auto req_ts_sv = dec(timestamp_ms / 1000U, req_ts_buf);
        if (salt_sv.empty() || maker_sv.empty() || taker_sv.empty()
            || ts_sv.empty() || req_ts_sv.empty()) return 68;

        clob_eip712::ExchangeV2OrderView order{};
        order.salt_decimal = salt_sv;
        order.maker = "0x1111111111111111111111111111111111111111";
        order.signer = order.maker;
        order.token_id_decimal = "1234";
        order.maker_amount_decimal = maker_sv;
        order.taker_amount_decimal = taker_sv;
        order.side = 0;
        order.signature_type = 3;
        order.timestamp_decimal = ts_sv;
        order.metadata_hex = "0x0000000000000000000000000000000000000000000000000000000000000000";
        order.builder_hex = order.metadata_hex;
        std::array<char, poly1271::kWrappedSignatureHexChars> signature{};
        if (!poly1271::sign_poly1271_hex(hasher, signer, order, signature)) return 69;
        const auto t2 = ns_now();
        clob_wire::MarketOrderDynamicView dynamic{};
        dynamic.maker_amount = maker_sv;
        dynamic.salt_decimal = salt_sv;
        dynamic.signature = {signature.data(), signature.size()};
        dynamic.taker_amount = taker_sv;
        dynamic.timestamp_ms = ts_sv;
        std::array<char, 8192> frame{};
        if (post.build(dynamic, req_ts_sv, frame) == 0) return 70;
        const auto t3 = ns_now();

        amount_ns.push_back(t1 - t0);
        sign_ns.push_back(t2 - t1);
        frame_ns.push_back(t3 - t2);
        total_ns.push_back(t3 - t0);
    }

    std::cout << "{\"schema\":\"polymarket_v7_native_clob_full_prewire_bench_v1\","
              << "\"paper_only\":true,\"real_order_submission\":false,"
              << "\"samples\":" << samples << ",\"latency_ns\":{";
    print_dist("amounts", amount_ns); std::cout << ',';
    print_dist("poly1271_sign", sign_ns); std::cout << ',';
    print_dist("zero_copy_post", frame_ns); std::cout << ',';
    print_dist("total", total_ns);
    std::cout << "}}\n";
    return 0;
}
