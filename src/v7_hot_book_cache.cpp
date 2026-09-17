#include "pm/v7_hot_book_cache.hpp"

#include <algorithm>
#include <array>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <sys/mman.h>
#include <unistd.h>

namespace pm::v7 {
namespace {
constexpr std::size_t kMarketOffset = 64;
constexpr std::size_t kMarketBytes = 96;
constexpr std::size_t kTokenOffset = 160;
constexpr std::size_t kTokenBytes = 96;
constexpr std::size_t kBidOffset = 256;
constexpr std::size_t kAskOffset = 376;
constexpr std::size_t kLevelBytes = 12;

void write_u32(std::byte* out, std::size_t offset, std::uint32_t value) noexcept {
    std::memcpy(out + offset, &value, sizeof(value));
}
void write_u64(std::byte* out, std::size_t offset, std::uint64_t value) noexcept {
    std::memcpy(out + offset, &value, sizeof(value));
}
void write_i64(std::byte* out, std::size_t offset, std::int64_t value) noexcept {
    std::memcpy(out + offset, &value, sizeof(value));
}
void write_i32(std::byte* out, std::size_t offset, std::int32_t value) noexcept {
    std::memcpy(out + offset, &value, sizeof(value));
}
void write_text(std::byte* out, std::size_t offset, std::size_t capacity,
                std::string_view value) noexcept {
    std::memset(out + offset, 0, capacity);
    const auto size = std::min(capacity, value.size());
    if (size > 0) std::memcpy(out + offset, value.data(), size);
}
}

HotBookCacheWriter::HotBookCacheWriter(const std::filesystem::path& path,
                                       std::string_view model_sha,
                                       std::size_t max_instrument_handle) {
    if (model_sha.size() != 40 || max_instrument_handle == 0
        || max_instrument_handle >= kHotBookCacheMaxSlots) {
        throw std::invalid_argument("invalid hot book cache identity/capacity");
    }
    std::filesystem::create_directories(path.parent_path());
    fd_ = ::open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd_ < 0) throw std::runtime_error("cannot open hot book cache");
    max_handle_ = max_instrument_handle;
    bytes_ = kHotBookCacheHeaderBytes + (max_handle_ + 1) * kHotBookCacheSlotBytes;
    if (::ftruncate(fd_, static_cast<off_t>(bytes_)) != 0) {
        ::close(fd_); fd_ = -1; throw std::runtime_error("cannot size hot book cache");
    }
    void* raw = ::mmap(nullptr, bytes_, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, 0);
    if (raw == MAP_FAILED) {
        ::close(fd_); fd_ = -1; throw std::runtime_error("cannot mmap hot book cache");
    }
    mapping_ = static_cast<std::byte*>(raw);
    std::memset(mapping_, 0, bytes_);
    std::memcpy(mapping_, kHotBookCacheMagic.data(), kHotBookCacheMagic.size());
    write_u32(mapping_, 8, kHotBookCacheVersion);
    write_u32(mapping_, 12, static_cast<std::uint32_t>(max_handle_));
    write_text(mapping_, 16, 40, model_sha);
    sequences_.resize(max_handle_ + 1, 0);
}

HotBookCacheWriter::~HotBookCacheWriter() {
    if (mapping_ != nullptr) ::munmap(mapping_, bytes_);
    if (fd_ >= 0) ::close(fd_);
}

bool HotBookCacheWriter::publish(std::uint64_t instrument_handle,
                                 std::string_view market_id,
                                 std::string_view token_id,
                                 const BookHotSnapshot& book,
                                 std::int64_t receive_wall_ms,
                                 std::int64_t min_order_size_microunits) noexcept {
    if (mapping_ == nullptr || instrument_handle == 0 || instrument_handle > max_handle_
        || market_id.empty() || market_id.size() > kMarketBytes
        || token_id.empty() || token_id.size() > kTokenBytes
        || receive_wall_ms <= 0 || book.bid_level_count > kHotDepthLevels
        || book.ask_level_count > kHotDepthLevels || min_order_size_microunits <= 0) {
        failures_.fetch_add(1, std::memory_order_relaxed); return false;
    }
    auto* slot = mapping_ + kHotBookCacheHeaderBytes
        + static_cast<std::size_t>(instrument_handle) * kHotBookCacheSlotBytes;
    auto& sequence = sequences_[instrument_handle];
    const std::uint64_t odd = sequence + 1;
    __atomic_store_n(reinterpret_cast<std::uint64_t*>(slot), odd, __ATOMIC_RELEASE);
    std::memset(slot + 8, 0, kHotBookCacheSlotBytes - 8);
    write_u64(slot, 8, instrument_handle);
    write_u64(slot, 16, book.state_version);
    write_i64(slot, 24, book.exchange_event_ns);
    write_i64(slot, 32, book.receive_monotonic_ns);
    write_i64(slot, 40, receive_wall_ms);
    write_i32(slot, 48, book.tick_size_e4);
    slot[52] = static_cast<std::byte>(book.bid_level_count);
    slot[53] = static_cast<std::byte>(book.ask_level_count);
    slot[54] = static_cast<std::byte>(book.lineage_continuous);
    slot[55] = static_cast<std::byte>(book.valid);
    slot[56] = static_cast<std::byte>(market_id.size());
    slot[57] = static_cast<std::byte>(token_id.size());
    write_text(slot, kMarketOffset, kMarketBytes, market_id);
    write_text(slot, kTokenOffset, kTokenBytes, token_id);
    for (std::size_t i = 0; i < book.bid_level_count; ++i) {
        write_i32(slot, kBidOffset + i * kLevelBytes, book.bid_levels[i].price_e4);
        write_i64(slot, kBidOffset + i * kLevelBytes + 4, book.bid_levels[i].quantity_microunits);
    }
    for (std::size_t i = 0; i < book.ask_level_count; ++i) {
        write_i32(slot, kAskOffset + i * kLevelBytes, book.ask_levels[i].price_e4);
        write_i64(slot, kAskOffset + i * kLevelBytes + 4, book.ask_levels[i].quantity_microunits);
    }
    write_i64(slot, 496, min_order_size_microunits);
    sequence = odd + 1;
    __atomic_store_n(reinterpret_cast<std::uint64_t*>(slot), sequence, __ATOMIC_RELEASE);
    publications_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

} // namespace pm::v7
