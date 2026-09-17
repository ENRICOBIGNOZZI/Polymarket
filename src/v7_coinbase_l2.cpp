#include "pm/v7_coinbase_l2.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <map>
#include <memory>
#include <memory_resource>
#include <new>

namespace pm::v7::external_fair {
namespace {
constexpr double kEps = 1e-12;
constexpr std::size_t kNodeBlockBytes = 256;
constexpr std::size_t kNodeBlocks = kCoinbaseL2MaxLevelsPerSide + 32;

[[nodiscard]] bool finite_positive(double value) noexcept {
    return std::isfinite(value) && value > 0.0;
}
[[nodiscard]] double imbalance(double bid, double ask) noexcept {
    const double total = bid + ask;
    return total > kEps ? (bid - ask) / total : 0.0;
}

class FixedNodeResource final : public std::pmr::memory_resource {
public:
    FixedNodeResource()
        : memory_(std::make_unique<std::byte[]>(kNodeBlockBytes * kNodeBlocks)) {}

private:
    void* do_allocate(std::size_t bytes, std::size_t alignment) override {
        if (bytes > kNodeBlockBytes || alignment > alignof(std::max_align_t)) throw std::bad_alloc();
        if (free_ != nullptr) {
            void* result = free_;
            free_ = *static_cast<void**>(free_);
            return result;
        }
        if (next_ >= kNodeBlocks) throw std::bad_alloc();
        return memory_.get() + (next_++ * kNodeBlockBytes);
    }
    void do_deallocate(void* pointer, std::size_t, std::size_t) override {
        *static_cast<void**>(pointer) = free_;
        free_ = pointer;
    }
    bool do_is_equal(const std::pmr::memory_resource& other) const noexcept override {
        return this == &other;
    }

    std::unique_ptr<std::byte[]> memory_;
    void* free_ = nullptr;
    std::size_t next_ = 0;
};

template <class Map>
[[nodiscard]] bool assign_levels(Map& target, std::span<const CoinbaseDepthLevel> input) noexcept {
    if (input.empty() || input.size() > kCoinbaseL2MaxLevelsPerSide) return false;
    target.clear();
    try {
        for (const auto& level : input) {
            if (!finite_positive(level.price) || !finite_positive(level.quantity)) {
                target.clear();
                return false;
            }
            if (!target.emplace(level.price, level.quantity).second) {
                target.clear();
                return false;
            }
        }
    } catch (const std::bad_alloc&) {
        target.clear();
        return false;
    } catch (...) {
        target.clear();
        return false;
    }
    return true;
}

template <class Map>
[[nodiscard]] bool mutate_level(Map& target, double price, double quantity) noexcept {
    if (!finite_positive(price) || !std::isfinite(quantity) || quantity < 0.0) return false;
    const auto found = target.find(price);
    if (found != target.end()) {
        if (quantity == 0.0) target.erase(found);
        else found->second = quantity;
        return true;
    }
    if (quantity == 0.0) return true;
    try {
        target.emplace(price, quantity);
        return true;
    } catch (...) {
        return false;
    }
}

template <class Map>
[[nodiscard]] double level_sum(const Map& levels, std::size_t requested) noexcept {
    double result = 0.0;
    std::size_t used = 0;
    for (const auto& [_, quantity] : levels) {
        if (used++ == requested) break;
        result += quantity;
    }
    return result;
}
} // namespace

struct CoinbaseL2Book::Storage {
    FixedNodeResource bid_resource{};
    FixedNodeResource ask_resource{};
    std::pmr::map<double, double, std::greater<double>> bids{&bid_resource};
    std::pmr::map<double, double, std::less<double>> asks{&ask_resource};
};

CoinbaseL2Book::CoinbaseL2Book() : storage_(std::make_unique<Storage>()) {}
CoinbaseL2Book::~CoinbaseL2Book() = default;

bool CoinbaseL2Book::install_snapshot(const CoinbaseDepthSnapshot& snapshot) {
    return install_snapshot(snapshot.local_receive_monotonic_ns, snapshot.bids, snapshot.asks);
}

bool CoinbaseL2Book::install_snapshot(
    std::int64_t receive_ns,
    std::span<const CoinbaseDepthLevel> bids,
    std::span<const CoinbaseDepthLevel> asks) noexcept {
    if (receive_ns <= 0 || !assign_levels(storage_->bids, bids)
        || !assign_levels(storage_->asks, asks) || !uncrossed()) {
        gap();
        return false;
    }
    update_count_ = 0;
    latest_receive_monotonic_ns_ = receive_ns;
    state_ = CoinbaseL2State::Live;
    return true;
}

bool CoinbaseL2Book::apply_update(const CoinbaseDepthUpdate& update) {
    return apply_update(update.local_receive_monotonic_ns, update.changes);
}

bool CoinbaseL2Book::apply_update(
    std::int64_t receive_ns,
    std::span<const CoinbaseDepthChange> changes) noexcept {
    if (state_ != CoinbaseL2State::Live || receive_ns <= 0 || changes.empty()) {
        gap();
        return false;
    }
    for (const auto& change : changes) {
        const bool ok = change.bid
            ? mutate_level(storage_->bids, change.price, change.quantity)
            : mutate_level(storage_->asks, change.price, change.quantity);
        if (!ok) {
            gap();
            return false;
        }
    }
    if (!uncrossed()) {
        gap();
        return false;
    }
    ++update_count_;
    latest_receive_monotonic_ns_ = receive_ns;
    return true;
}

bool CoinbaseL2Book::uncrossed() const noexcept {
    return !storage_->bids.empty() && !storage_->asks.empty()
        && storage_->bids.begin()->first < storage_->asks.begin()->first;
}

void CoinbaseL2Book::gap() noexcept {
    storage_->bids.clear();
    storage_->asks.clear();
    latest_receive_monotonic_ns_ = 0;
    state_ = CoinbaseL2State::Gapped;
}

void CoinbaseL2Book::begin_recovery() noexcept {
    storage_->bids.clear();
    storage_->asks.clear();
    update_count_ = 0;
    latest_receive_monotonic_ns_ = 0;
    state_ = CoinbaseL2State::AwaitingSnapshot;
}

CoinbaseL2Metrics CoinbaseL2Book::metrics() const noexcept {
    CoinbaseL2Metrics out;
    out.update_count = update_count_;
    out.latest_receive_monotonic_ns = latest_receive_monotonic_ns_;
    out.state = state_;
    out.bid_levels = static_cast<std::uint32_t>(std::min<std::size_t>(
        storage_->bids.size(), std::numeric_limits<std::uint32_t>::max()));
    out.ask_levels = static_cast<std::uint32_t>(std::min<std::size_t>(
        storage_->asks.size(), std::numeric_limits<std::uint32_t>::max()));
    if (state_ != CoinbaseL2State::Live || !uncrossed()) return out;
    out.best_bid = storage_->bids.begin()->first;
    out.best_ask = storage_->asks.begin()->first;
    out.mid = 0.5 * (out.best_bid + out.best_ask);
    out.bid_depth_l1 = level_sum(storage_->bids, 1);
    out.ask_depth_l1 = level_sum(storage_->asks, 1);
    out.bid_depth_l5 = level_sum(storage_->bids, 5);
    out.ask_depth_l5 = level_sum(storage_->asks, 5);
    out.bid_depth_l10 = level_sum(storage_->bids, 10);
    out.ask_depth_l10 = level_sum(storage_->asks, 10);
    out.bid_depth_l20 = level_sum(storage_->bids, 20);
    out.ask_depth_l20 = level_sum(storage_->asks, 20);
    const double l1 = out.bid_depth_l1 + out.ask_depth_l1;
    out.microprice = l1 > kEps
        ? (out.best_ask * out.bid_depth_l1 + out.best_bid * out.ask_depth_l1) / l1
        : out.mid;
    out.spread_bps = (out.best_ask - out.best_bid) / out.mid * 10'000.0;
    out.imbalance_l1 = imbalance(out.bid_depth_l1, out.ask_depth_l1);
    out.imbalance_l5 = imbalance(out.bid_depth_l5, out.ask_depth_l5);
    out.imbalance_l10 = imbalance(out.bid_depth_l10, out.ask_depth_l10);
    out.valid = 1;
    return out;
}

} // namespace pm::v7::external_fair
