#include "pm/v7_clob_wire.hpp"

#include <openssl/crypto.h>
#include <openssl/sha.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <new>

namespace pm::v7::clob_wire {

struct L2HmacState {
    SHA256_CTX inner_seed{};
    SHA256_CTX outer_seed{};
};

namespace {

class BufferWriter final {
public:
    explicit BufferWriter(std::span<char> output) noexcept : output_(output) {}

    bool append(std::string_view value) noexcept {
        if (!ok_ || value.size() > output_.size() - size_) {
            ok_ = false;
            return false;
        }
        if (!value.empty()) {
            std::memcpy(output_.data() + size_, value.data(), value.size());
            size_ += value.size();
        }
        return true;
    }

    // serialize_post_market_order() validates every dynamic atom once before
    // writing. Re-scanning long signatures/token IDs here only duplicates work.
    bool quoted_validated(std::string_view value) noexcept {
        return append("\"") && append(value) && append("\"");
    }

    bool signature_type_validated(std::uint8_t value) noexcept {
        const char digit = static_cast<char>('0' + value);
        return append(std::string_view(&digit, 1));
    }

    [[nodiscard]] std::size_t size() const noexcept { return ok_ ? size_ : 0; }

    static bool json_atom(std::string_view value) noexcept {
        if (value.empty()) return false;
        for (const unsigned char c : value) {
            if (c < 0x20U || c > 0x7eU || c == '"' || c == '\\') return false;
        }
        return true;
    }

private:
    std::span<char> output_;
    std::size_t size_ = 0;
    bool ok_ = true;
};

bool decimal(std::string_view value) noexcept {
    if (value.empty()) return false;
    return std::all_of(value.begin(), value.end(), [](unsigned char c) { return c >= '0' && c <= '9'; });
}

bool valid_order(const PostMarketOrderView& request) noexcept {
    const auto& order = request.order;
    if (!BufferWriter::json_atom(request.owner)) return false;
    if (request.order_type != MarketOrderType::FAK && request.order_type != MarketOrderType::FOK) return false;
    // Current CLOB direct-market-order contract requires expiration="0".
    if (order.expiration != "0") return false;
    if (order.side != "BUY" && order.side != "SELL") return false;
    if (order.signature_type > 3) return false;
    if (!decimal(order.maker_amount) || !decimal(order.taker_amount)
        || !decimal(order.salt_decimal) || !decimal(order.timestamp_ms)) return false;
    const std::array<std::string_view, 6> atoms{
        order.builder, order.maker, order.metadata, order.signature,
        order.signer, order.token_id};
    return std::all_of(atoms.begin(), atoms.end(), BufferWriter::json_atom);
}

int base64_value(unsigned char c) noexcept {
    if (c >= 'A' && c <= 'Z') return static_cast<int>(c - 'A');
    if (c >= 'a' && c <= 'z') return static_cast<int>(c - 'a') + 26;
    if (c >= '0' && c <= '9') return static_cast<int>(c - '0') + 52;
    if (c == '+' || c == '-') return 62;
    if (c == '/' || c == '_') return 63;
    return -1;
}

bool decode_base64(std::string_view input, std::span<unsigned char> output,
                   std::size_t& written) noexcept {
    written = 0;
    if (input.empty()) return false;
    std::array<int, 4> q{};
    std::size_t qn = 0;
    bool padded = false;

    const auto emit = [&](std::size_t count) noexcept -> bool {
        if (count < 2 || q[0] < 0 || q[1] < 0) return false;
        const bool pad2 = count > 2 && q[2] == -2;
        const bool pad3 = count > 3 && q[3] == -2;
        if (pad2 && (!pad3 || count != 4)) return false;
        if (count > 2 && q[2] < 0 && !pad2) return false;
        if (count > 3 && q[3] < 0 && !pad3) return false;
        const std::size_t need = 1 + ((!pad2 && count >= 3) ? 1U : 0U)
            + ((!pad2 && !pad3 && count == 4) ? 1U : 0U);
        if (need > output.size() - written) return false;
        output[written++] = static_cast<unsigned char>((q[0] << 2) | (q[1] >> 4));
        if (!pad2 && count >= 3) {
            output[written++] = static_cast<unsigned char>(((q[1] & 0x0f) << 4) | (q[2] >> 2));
        }
        if (!pad2 && !pad3 && count == 4) {
            output[written++] = static_cast<unsigned char>(((q[2] & 0x03) << 6) | q[3]);
        }
        return true;
    };

    for (const unsigned char c : input) {
        if (padded) return false;
        if (c == '=') q[qn++] = -2;
        else {
            const int value = base64_value(c);
            if (value < 0) return false;
            q[qn++] = value;
        }
        if (qn == 4) {
            const bool has_padding = q[2] == -2 || q[3] == -2;
            if (!emit(4)) return false;
            qn = 0;
            if (has_padding) padded = true;
        }
    }
    if (qn != 0) {
        if (padded || qn == 1) return false;
        if (!emit(qn)) return false;
    }
    return written > 0;
}

std::size_t encode_base64url(std::span<const unsigned char> input,
                             std::span<char> output) noexcept {
    static constexpr std::string_view alphabet =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    const std::size_t required = 4U * ((input.size() + 2U) / 3U);
    if (output.size() < required) return 0;
    std::size_t in = 0, out = 0;
    while (in + 3 <= input.size()) {
        const std::uint32_t value = (static_cast<std::uint32_t>(input[in]) << 16)
            | (static_cast<std::uint32_t>(input[in + 1]) << 8)
            | static_cast<std::uint32_t>(input[in + 2]);
        output[out++] = alphabet[(value >> 18) & 0x3fU];
        output[out++] = alphabet[(value >> 12) & 0x3fU];
        output[out++] = alphabet[(value >> 6) & 0x3fU];
        output[out++] = alphabet[value & 0x3fU];
        in += 3;
    }
    const std::size_t remain = input.size() - in;
    if (remain == 1) {
        const std::uint32_t value = static_cast<std::uint32_t>(input[in]) << 16;
        output[out++] = alphabet[(value >> 18) & 0x3fU];
        output[out++] = alphabet[(value >> 12) & 0x3fU];
        output[out++] = '=';
        output[out++] = '=';
    } else if (remain == 2) {
        const std::uint32_t value = (static_cast<std::uint32_t>(input[in]) << 16)
            | (static_cast<std::uint32_t>(input[in + 1]) << 8);
        output[out++] = alphabet[(value >> 18) & 0x3fU];
        output[out++] = alphabet[(value >> 12) & 0x3fU];
        output[out++] = alphabet[(value >> 6) & 0x3fU];
        output[out++] = '=';
    }
    return out;
}

bool update_sha256(SHA256_CTX& ctx, std::string_view value) noexcept {
    return SHA256_Update(&ctx, value.data(), value.size()) == 1;
}

} // namespace

std::size_t serialize_post_market_order(const PostMarketOrderView& request,
                                        std::span<char> output) noexcept {
    if (!valid_order(request)) return 0;
    const auto& order = request.order;
    BufferWriter w(output);
    w.append("{\"deferExec\":false,\"order\":{\"builder\":"); w.quoted_validated(order.builder);
    w.append(",\"expiration\":"); w.quoted_validated(order.expiration);
    w.append(",\"maker\":"); w.quoted_validated(order.maker);
    w.append(",\"makerAmount\":"); w.quoted_validated(order.maker_amount);
    w.append(",\"metadata\":"); w.quoted_validated(order.metadata);
    w.append(",\"salt\":"); w.append(order.salt_decimal);
    w.append(",\"side\":"); w.quoted_validated(order.side);
    w.append(",\"signature\":"); w.quoted_validated(order.signature);
    w.append(",\"signatureType\":"); w.signature_type_validated(order.signature_type);
    w.append(",\"signer\":"); w.quoted_validated(order.signer);
    w.append(",\"takerAmount\":"); w.quoted_validated(order.taker_amount);
    w.append(",\"timestamp\":"); w.quoted_validated(order.timestamp_ms);
    w.append(",\"tokenId\":"); w.quoted_validated(order.token_id);
    w.append("},\"orderType\":\"");
    w.append(request.order_type == MarketOrderType::FAK ? "FAK" : "FOK");
    w.append("\",\"owner\":"); w.quoted_validated(request.owner); w.append("}");
    return w.size();
}

L2HmacSigner::L2HmacSigner(std::string_view base64_secret) noexcept {
    std::array<unsigned char, 128> decoded{};
    std::size_t decoded_size = 0;
    if (!decode_base64(base64_secret, decoded, decoded_size)) return;

    std::array<unsigned char, 64> key_block{};
    if (decoded_size > key_block.size()) {
        std::array<unsigned char, SHA256_DIGEST_LENGTH> reduced{};
        if (SHA256(decoded.data(), decoded_size, reduced.data()) == nullptr) {
            OPENSSL_cleanse(decoded.data(), decoded.size());
            return;
        }
        std::memcpy(key_block.data(), reduced.data(), reduced.size());
        OPENSSL_cleanse(reduced.data(), reduced.size());
    } else {
        std::memcpy(key_block.data(), decoded.data(), decoded_size);
    }
    OPENSSL_cleanse(decoded.data(), decoded.size());

    std::array<unsigned char, 64> inner_pad{};
    std::array<unsigned char, 64> outer_pad{};
    for (std::size_t i = 0; i < key_block.size(); ++i) {
        inner_pad[i] = static_cast<unsigned char>(key_block[i] ^ 0x36U);
        outer_pad[i] = static_cast<unsigned char>(key_block[i] ^ 0x5cU);
    }
    OPENSSL_cleanse(key_block.data(), key_block.size());

    auto* candidate = new (std::nothrow) L2HmacState;
    if (candidate == nullptr) {
        OPENSSL_cleanse(inner_pad.data(), inner_pad.size());
        OPENSSL_cleanse(outer_pad.data(), outer_pad.size());
        return;
    }
    const bool initialized = SHA256_Init(&candidate->inner_seed) == 1
        && SHA256_Update(&candidate->inner_seed, inner_pad.data(), inner_pad.size()) == 1
        && SHA256_Init(&candidate->outer_seed) == 1
        && SHA256_Update(&candidate->outer_seed, outer_pad.data(), outer_pad.size()) == 1;
    OPENSSL_cleanse(inner_pad.data(), inner_pad.size());
    OPENSSL_cleanse(outer_pad.data(), outer_pad.size());
    if (!initialized) {
        OPENSSL_cleanse(candidate, sizeof(*candidate));
        delete candidate;
        return;
    }
    state_ = candidate;
}

L2HmacSigner::~L2HmacSigner() {
    if (state_ != nullptr) {
        OPENSSL_cleanse(state_, sizeof(*state_));
        delete state_;
        state_ = nullptr;
    }
}

std::size_t L2HmacSigner::sign(std::string_view request_timestamp,
                               std::string_view exact_body,
                               std::span<char> output) noexcept {
    if (state_ == nullptr || request_timestamp.empty() || exact_body.empty()) return 0;

    SHA256_CTX inner{};
    std::memcpy(&inner, &state_->inner_seed, sizeof(inner));
    if (!update_sha256(inner, request_timestamp)
        || !update_sha256(inner, "POST")
        || !update_sha256(inner, "/order")
        || !update_sha256(inner, exact_body)) {
        OPENSSL_cleanse(&inner, sizeof(inner));
        return 0;
    }

    std::array<unsigned char, SHA256_DIGEST_LENGTH> inner_digest{};
    if (SHA256_Final(inner_digest.data(), &inner) != 1) {
        OPENSSL_cleanse(&inner, sizeof(inner));
        return 0;
    }
    OPENSSL_cleanse(&inner, sizeof(inner));

    SHA256_CTX outer{};
    std::memcpy(&outer, &state_->outer_seed, sizeof(outer));
    if (SHA256_Update(&outer, inner_digest.data(), inner_digest.size()) != 1) {
        OPENSSL_cleanse(inner_digest.data(), inner_digest.size());
        OPENSSL_cleanse(&outer, sizeof(outer));
        return 0;
    }
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    const bool ok = SHA256_Final(digest.data(), &outer) == 1;
    OPENSSL_cleanse(inner_digest.data(), inner_digest.size());
    OPENSSL_cleanse(&outer, sizeof(outer));
    if (!ok) return 0;

    const auto written = encode_base64url(digest, output);
    OPENSSL_cleanse(digest.data(), digest.size());
    return written;
}

} // namespace pm::v7::clob_wire
