#include "pm/v7_clob_prepared_body.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstring>
#include <limits>

namespace pm::v7::clob_prepared_body {
namespace {

[[nodiscard]] bool json_atom(std::string_view value) noexcept {
    if (value.empty()) return false;
    for (const unsigned char c : value) {
        if (c < 0x20U || c > 0x7eU || c == '"' || c == '\\') return false;
    }
    return true;
}

[[nodiscard]] bool decimal(std::string_view value) noexcept {
    return !value.empty() && std::all_of(value.begin(), value.end(),
        [](unsigned char c) { return c >= '0' && c <= '9'; });
}

class StorageWriter final {
public:
    explicit StorageWriter(std::span<char> output) noexcept : output_(output) {}
    [[nodiscard]] std::size_t size() const noexcept { return ok_ ? size_ : 0; }
    [[nodiscard]] bool ok() const noexcept { return ok_; }
    void append(std::string_view value) noexcept {
        if (!ok_ || value.size() > output_.size() - size_) { ok_ = false; return; }
        if (!value.empty()) std::memcpy(output_.data() + size_, value.data(), value.size());
        size_ += value.size();
    }
    void quoted(std::string_view value) noexcept {
        append("\""); append(value); append("\"");
    }
    void integer(std::uint8_t value) noexcept {
        std::array<char, 4> buffer{};
        const auto [end, error] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
        if (error != std::errc{}) { ok_ = false; return; }
        append(std::string_view(buffer.data(), static_cast<std::size_t>(end - buffer.data())));
    }
private:
    std::span<char> output_;
    std::size_t size_ = 0;
    bool ok_ = true;
};

[[nodiscard]] bool append_output(std::span<char> output, std::size_t& position,
                                 std::string_view value) noexcept {
    if (value.size() > output.size() - position) return false;
    if (!value.empty()) std::memcpy(output.data() + position, value.data(), value.size());
    position += value.size();
    return true;
}

} // namespace

PreparedPostMarketOrderBody::PreparedPostMarketOrderBody(const StaticOrderView& fixed) noexcept {
    using pm::v7::clob_wire::MarketOrderType;
    if (!json_atom(fixed.builder) || !json_atom(fixed.maker) || !json_atom(fixed.metadata)
        || !json_atom(fixed.side) || !json_atom(fixed.signer) || !json_atom(fixed.token_id)
        || !json_atom(fixed.owner) || fixed.expiration != "0"
        || (fixed.side != "BUY" && fixed.side != "SELL") || fixed.signature_type > 3
        || (fixed.order_type != MarketOrderType::FAK && fixed.order_type != MarketOrderType::FOK)) return;
    StorageWriter writer(storage_);
    const auto start = [&](std::size_t index) noexcept {
        offsets_[index] = static_cast<std::uint16_t>(writer.size());
    };
    const auto finish = [&](std::size_t index) noexcept {
        if (!writer.ok()) return false;
        const auto end = writer.size();
        const auto begin = static_cast<std::size_t>(offsets_[index]);
        if (end < begin || end - begin > 0xffffU) return false;
        sizes_[index] = static_cast<std::uint16_t>(end - begin);
        return true;
    };

    start(0);
    writer.append("{\"deferExec\":false,\"order\":{\"builder\":"); writer.quoted(fixed.builder);
    writer.append(",\"expiration\":"); writer.quoted(fixed.expiration);
    writer.append(",\"maker\":"); writer.quoted(fixed.maker);
    writer.append(",\"makerAmount\":\"");
    if (!finish(0)) return;

    start(1);
    writer.append("\",\"metadata\":"); writer.quoted(fixed.metadata);
    writer.append(",\"salt\":");
    if (!finish(1)) return;

    start(2);
    writer.append(",\"side\":"); writer.quoted(fixed.side);
    writer.append(",\"signature\":\"");
    if (!finish(2)) return;
    start(3);
    writer.append("\",\"signatureType\":"); writer.integer(fixed.signature_type);
    writer.append(",\"signer\":"); writer.quoted(fixed.signer);
    writer.append(",\"takerAmount\":\"");
    if (!finish(3)) return;

    start(4);
    writer.append("\",\"timestamp\":\"");
    if (!finish(4)) return;

    start(5);
    writer.append("\",\"tokenId\":"); writer.quoted(fixed.token_id);
    writer.append("},\"orderType\":\"");
    writer.append(fixed.order_type == MarketOrderType::FAK ? "FAK" : "FOK");
    writer.append("\",\"owner\":"); writer.quoted(fixed.owner); writer.append("}");
    if (!finish(5)) return;
    valid_ = true;
}

std::size_t PreparedPostMarketOrderBody::serialize(
    std::string_view maker_amount,
    std::string_view salt_decimal,
    std::string_view signature,
    std::string_view taker_amount,
    std::string_view timestamp_ms,
    std::span<char> output) const noexcept {
    if (!valid_ || !decimal(maker_amount) || !decimal(salt_decimal)
        || !json_atom(signature) || !decimal(taker_amount) || !decimal(timestamp_ms)) return 0;
    const std::array<std::string_view, 5> dynamic{
        maker_amount, salt_decimal, signature, taker_amount, timestamp_ms};
    std::size_t required = 0;
    const auto add_required = [&](std::size_t amount) noexcept -> bool {
        if (amount > std::numeric_limits<std::size_t>::max() - required) return false;
        required += amount;
        return true;
    };
    for (const auto size : sizes_) if (!add_required(size)) return 0;
    for (const auto value : dynamic) if (!add_required(value.size())) return 0;
    if (required > output.size()) return 0;

    std::size_t position = 0;
    for (std::size_t index = 0; index < kSegments; ++index) {
        const auto segment = std::string_view(
            storage_.data() + offsets_[index], sizes_[index]);
        if (!append_output(output, position, segment)) return 0;
        if (index < dynamic.size()
            && !append_output(output, position, dynamic[index])) return 0;
    }
    return position == required ? position : 0;
}

} // namespace pm::v7::clob_prepared_body
