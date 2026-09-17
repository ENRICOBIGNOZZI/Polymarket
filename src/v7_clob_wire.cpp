#include "pm/v7_clob_wire.hpp"

#include <openssl/core_names.h>
#include <openssl/evp.h>
#include <openssl/params.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cstring>

namespace pm::v7::clob_wire {
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

    bool quoted(std::string_view value) noexcept {
        if (!json_atom(value)) {
            ok_ = false;
            return false;
        }
        return append("\"") && append(value) && append("\"");
    }

    bool integer(unsigned value) noexcept {
        std::array<char, 16> buffer{};
        const auto [end, ec] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
        return ec == std::errc{} && append(std::string_view(buffer.data(), static_cast<std::size_t>(end - buffer.data())));
    }

    [[nodiscard]] bool ok() const noexcept { return ok_; }
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
    if (request.owner.empty() || !BufferWriter::json_atom(request.owner)) return false;
    if (request.order_type != MarketOrderType::FAK && request.order_type != MarketOrderType::FOK) return false;
    // Current CLOB direct-market-order contract requires expiration="0".
    if (order.expiration != "0") return false;
    if (order.side != "BUY" && order.side != "SELL") return false;
    if (order.signature_type > 3) return false;
    if (!decimal(order.maker_amount) || !decimal(order.taker_amount)
        || !decimal(order.salt_decimal) || !decimal(order.timestamp_ms)) return false;
    const std::array<std::string_view, 7> atoms{
        order.builder, order.maker, order.metadata, order.signature,
        order.signer, order.token_id, order.side};
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

} // namespace

std::size_t serialize_post_market_order(const PostMarketOrderView& request,
                                        std::span<char> output) noexcept {
    if (!valid_order(request)) return 0;
    const auto& order = request.order;
    BufferWriter w(output);
    w.append("{\"deferExec\":false,\"order\":{\"builder\":"); w.quoted(order.builder);
    w.append(",\"expiration\":"); w.quoted(order.expiration);
    w.append(",\"maker\":"); w.quoted(order.maker);
    w.append(",\"makerAmount\":"); w.quoted(order.maker_amount);
    w.append(",\"metadata\":"); w.quoted(order.metadata);
    w.append(",\"salt\":"); w.append(order.salt_decimal);
    w.append(",\"side\":"); w.quoted(order.side);
    w.append(",\"signature\":"); w.quoted(order.signature);
    w.append(",\"signatureType\":"); w.integer(order.signature_type);
    w.append(",\"signer\":"); w.quoted(order.signer);
    w.append(",\"takerAmount\":"); w.quoted(order.taker_amount);
    w.append(",\"timestamp\":"); w.quoted(order.timestamp_ms);
    w.append(",\"tokenId\":"); w.quoted(order.token_id);
    w.append("},\"orderType\":\"");
    w.append(request.order_type == MarketOrderType::FAK ? "FAK" : "FOK");
    w.append("\",\"owner\":"); w.quoted(request.owner); w.append("}");
    return w.size();
}

L2HmacSigner::L2HmacSigner(std::string_view base64_secret) noexcept {
    if (!decode_base64(base64_secret, key_, key_size_)) return;
    mac_ = EVP_MAC_fetch(nullptr, "HMAC", nullptr);
    if (mac_ == nullptr) return;
    ctx_ = EVP_MAC_CTX_new(mac_);
    if (ctx_ == nullptr) return;
    valid_ = true;
}

L2HmacSigner::~L2HmacSigner() {
    if (ctx_ != nullptr) EVP_MAC_CTX_free(ctx_);
    if (mac_ != nullptr) EVP_MAC_free(mac_);
    std::fill(key_.begin(), key_.end(), 0U);
    key_size_ = 0;
    ctx_ = nullptr;
    mac_ = nullptr;
    valid_ = false;
}

std::size_t L2HmacSigner::sign(std::string_view request_timestamp,
                               std::string_view exact_body,
                               std::span<char> output) noexcept {
    if (!valid_ || request_timestamp.empty() || exact_body.empty()) return 0;
    std::array<unsigned char, 32> digest{};
    std::size_t digest_size = 0;
    char digest_name[] = "SHA256";
    OSSL_PARAM params[] = {
        OSSL_PARAM_construct_utf8_string(OSSL_MAC_PARAM_DIGEST, digest_name, 0),
        OSSL_PARAM_construct_end(),
    };
    if (EVP_MAC_init(ctx_, key_.data(), key_size_, params) != 1) return 0;
    const auto update = [&](std::string_view value) noexcept {
        return EVP_MAC_update(ctx_, reinterpret_cast<const unsigned char*>(value.data()), value.size()) == 1;
    };
    if (!update(request_timestamp) || !update("POST") || !update("/order") || !update(exact_body)) return 0;
    if (EVP_MAC_final(ctx_, digest.data(), &digest_size, digest.size()) != 1
        || digest_size != digest.size()) return 0;
    return encode_base64url(std::span<const unsigned char>(digest.data(), digest_size), output);
}

} // namespace pm::v7::clob_wire
