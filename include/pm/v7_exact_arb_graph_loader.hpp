#pragma once

#include "pm/v7_exact_arb_graph_runtime.hpp"

#include <string>
#include <string_view>

namespace pm::v7::exact_arb_graph {

// Control-plane bindings, populated from the current subscribed selection and
// cold-start venue metadata. Never infer zero fees or an unlimited market life.
struct NativeTokenBinding {
    std::string token_id;
    std::uint32_t book_handle = 0;
    std::int32_t tick_size_e4 = 0;
    std::int64_t start_wall_ms = 0;
    std::int64_t end_wall_ms = 0;
    std::int64_t minimum_order_microunits = 0;
    std::int64_t fee_rate_nanos = -1;
    std::int64_t fee_exponent = -1;
};

struct LoadedNativeGeneration {
    OwnedGeneration generation;
    std::string canonical_payload;
    std::string bundle_sha256;
    std::int64_t valid_until_wall_ms = 0;
};

// OFF PATH ONLY. Throws on malformed, expired, mismatched or unproved input.
// Rational payoff verification proves the supplied finite-state theorem, NOT
// independent settlement semantics. The source graph's attestation trust model
// remains mandatory. A SHA-256 is integrity, not signature/authentication.
[[nodiscard]] LoadedNativeGeneration load_native_generation(
    std::string_view selection_json, std::string_view expected_model_sha,
    std::span<const NativeTokenBinding> bindings,
    std::int64_t now_wall_ms, std::int64_t now_monotonic_ns);

} // namespace pm::v7::exact_arb_graph
