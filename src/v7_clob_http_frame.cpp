#include "pm/v7_clob_http_frame.hpp"

#include <array>
#include <charconv>
#include <cstring>

namespace pm::v7::clob_http_frame {
namespace {
class Writer final {
public:
    explicit Writer(std::span<char> out) noexcept : out_(out) {}
    bool append(std::string_view s) noexcept {
        if (!ok_ || s.size() > out_.size() - size_) { ok_ = false; return false; }
        if (!s.empty()) std::memcpy(out_.data() + size_, s.data(), s.size());
        size_ += s.size();
        return true;
    }
    bool decimal(std::size_t value) noexcept {
        std::array<char, 32> buffer{};
        const auto [end, ec] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
        return ec == std::errc{} && append(std::string_view(buffer.data(), static_cast<std::size_t>(end - buffer.data())));
    }
    [[nodiscard]] std::size_t size() const noexcept { return ok_ ? size_ : 0; }
private:
    std::span<char> out_;
    std::size_t size_ = 0;
    bool ok_ = true;
};

bool header_value(std::string_view value) noexcept {
    if (value.empty()) return false;
    for (const unsigned char c : value) {
        // Reject all controls including CR/LF. Visible ASCII and spaces are
        // sufficient for current CLOB addresses/keys/passphrases/signatures.
        if (c < 0x20U || c > 0x7eU) return false;
    }
    return true;
}

bool decimal_value(std::string_view value) noexcept {
    if (value.empty()) return false;
    for (const unsigned char c : value) if (c < '0' || c > '9') return false;
    return true;
}
}

std::size_t serialize_post_order_http1(const L2AuthHeadersView& auth,
                                       std::string_view exact_body,
                                       std::span<char> output) noexcept {
    if (exact_body.empty()
        || !header_value(auth.address)
        || !header_value(auth.signature)
        || !decimal_value(auth.timestamp)
        || !header_value(auth.api_key)
        || !header_value(auth.passphrase)) return 0;

    Writer w(output);
    w.append("POST /order HTTP/1.1\r\n");
    w.append("Host: clob.polymarket.com\r\n");
    w.append("Content-Type: application/json\r\n");
    w.append("Accept: application/json\r\n");
    w.append("POLY_ADDRESS: "); w.append(auth.address); w.append("\r\n");
    w.append("POLY_SIGNATURE: "); w.append(auth.signature); w.append("\r\n");
    w.append("POLY_TIMESTAMP: "); w.append(auth.timestamp); w.append("\r\n");
    w.append("POLY_API_KEY: "); w.append(auth.api_key); w.append("\r\n");
    w.append("POLY_PASSPHRASE: "); w.append(auth.passphrase); w.append("\r\n");
    w.append("Content-Length: "); w.decimal(exact_body.size()); w.append("\r\n");
    w.append("Connection: keep-alive\r\n\r\n");
    w.append(exact_body);
    return w.size();
}

PreparedPostOrderHttp1::PreparedPostOrderHttp1(std::string_view address,
                                               std::string_view api_key,
                                               std::string_view passphrase) noexcept {
    if (!header_value(address) || !header_value(api_key) || !header_value(passphrase)) return;

    Writer prefix(prefix_);
    prefix.append("POST /order HTTP/1.1\r\n");
    prefix.append("Host: clob.polymarket.com\r\n");
    prefix.append("Content-Type: application/json\r\n");
    prefix.append("Accept: application/json\r\n");
    prefix.append("POLY_ADDRESS: "); prefix.append(address); prefix.append("\r\n");
    prefix.append("POLY_SIGNATURE: ");
    prefix_size_ = prefix.size();
    if (prefix_size_ == 0) return;

    Writer tail(after_timestamp_);
    tail.append("\r\nPOLY_API_KEY: "); tail.append(api_key); tail.append("\r\n");
    tail.append("POLY_PASSPHRASE: "); tail.append(passphrase); tail.append("\r\n");
    tail.append("Content-Length: ");
    after_timestamp_size_ = tail.size();
    valid_ = after_timestamp_size_ != 0;
}

std::size_t PreparedPostOrderHttp1::serialize(std::string_view signature,
                                              std::string_view timestamp,
                                              std::string_view exact_body,
                                              std::span<char> output) const noexcept {
    if (!valid_ || exact_body.empty() || !header_value(signature) || !decimal_value(timestamp)) return 0;

    Writer w(output);
    w.append(std::string_view(prefix_.data(), prefix_size_));
    w.append(signature);
    w.append("\r\nPOLY_TIMESTAMP: ");
    w.append(timestamp);
    w.append(std::string_view(after_timestamp_.data(), after_timestamp_size_));
    w.decimal(exact_body.size());
    w.append("\r\nConnection: keep-alive\r\n\r\n");
    w.append(exact_body);
    return w.size();
}

} // namespace pm::v7::clob_http_frame
