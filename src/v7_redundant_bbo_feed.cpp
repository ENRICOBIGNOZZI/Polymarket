#include "pm/v7_redundant_bbo_feed.hpp"

#include "pm/fast_ws.hpp"
#include "pm/v7_spsc.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <memory>
#include <stdexcept>
#include <string_view>
#include <utility>

namespace pm::v7::redundant_bbo {
namespace {
inline constexpr std::size_t kMaxUpdatesPerFrame = 256;
inline constexpr std::size_t kMaxDrainPerTry = 64;
inline constexpr std::string_view kDefaultEndpoint =
    "wss://ws-subscriptions-clob.polymarket.com/ws/market";
}

struct Feed::Impl {
    struct alignas(64) Lane final {
        SpscRing<Envelope, kQueueCapacity> queue{};
        std::atomic<std::uint64_t> generation{0};
        std::atomic<std::uint64_t> decoded_updates{0};
        std::atomic<std::uint64_t> queue_drops{0};
        std::atomic<std::uint64_t> decode_failures{0};
        std::uint64_t writer_generation = 0;
    };

    polymarket_bbo::Decoder decoder;
    std::array<Lane, kLaneCount> lanes{};
    std::array<std::unique_ptr<pm::fast::MarketWebSocketFeed>, kLaneCount> feeds{};
    Gate gate;
    std::array<Envelope, kLaneCount> heads{};
    std::array<std::uint8_t, kLaneCount> head_valid{};
    std::atomic<std::uint8_t> disabled_mask{0};
    std::atomic<bool> started{false};

    Impl(std::string url, const std::vector<polymarket_bbo::Binding>& bindings, Mode mode,
         int socket_busy_poll_us)
        : decoder(bindings), gate(mode) {
        if (url.empty()) url = std::string(kDefaultEndpoint);
        std::vector<std::string> assets;
        assets.reserve(bindings.size());
        for (const auto& b : bindings) assets.push_back(b.asset_id);
        std::sort(assets.begin(), assets.end());
        assets.erase(std::unique(assets.begin(), assets.end()), assets.end());
        if (assets.empty()) throw std::invalid_argument("redundant BBO feed requires assets");

        for (std::size_t lane = 0; lane < kLaneCount; ++lane) {
            feeds[lane] = std::make_unique<pm::fast::MarketWebSocketFeed>(
                url, assets, assets.size(),
                [this, lane](std::string_view payload,
                             const pm::fast::FeedReceiveStamp& stamp,
                             std::size_t) { on_frame(lane, payload, stamp.monotonic_ns); },
                [this, lane](std::size_t, std::string_view) { disable_lane(lane); },
                socket_busy_poll_us);
        }
    }

    void disable_lane(std::size_t lane) noexcept {
        disabled_mask.fetch_or(static_cast<std::uint8_t>(1U << lane), std::memory_order_release);
    }

    void on_frame(std::size_t lane, std::string_view payload, std::int64_t receive_ns) noexcept {
        const auto bit = static_cast<std::uint8_t>(1U << lane);
        if ((disabled_mask.load(std::memory_order_acquire) & bit) != 0) return;
        std::array<polymarket_bbo::Update, kMaxUpdatesPerFrame> decoded{};
        const auto result = decoder.decode(payload, receive_ns, decoded);
        if (result.invalid_frame != 0 || result.output_overflow != 0) {
            lanes[lane].decode_failures.fetch_add(1, std::memory_order_relaxed);
            disable_lane(lane);
            return;
        }
        if (result.output_count == 0) return;
        for (std::size_t i = 0; i < result.output_count; ++i) {
            Envelope envelope;
            envelope.update = decoded[i];
            envelope.lane = static_cast<std::uint8_t>(lane);
            if (!lanes[lane].queue.try_push(envelope)) {
                lanes[lane].queue_drops.fetch_add(1, std::memory_order_relaxed);
                disable_lane(lane);
                return;
            }
        }
        lanes[lane].decoded_updates.fetch_add(result.output_count, std::memory_order_relaxed);
        lanes[lane].generation.store(++lanes[lane].writer_generation, std::memory_order_release);
    }

    void discard_disabled(std::size_t lane) noexcept {
        Envelope ignored;
        head_valid[lane] = 0;
        while (lanes[lane].queue.try_pop(ignored)) {}
    }

    bool fill_head(std::size_t lane) noexcept {
        if (head_valid[lane] != 0) return true;
        const auto bit = static_cast<std::uint8_t>(1U << lane);
        if ((disabled_mask.load(std::memory_order_acquire) & bit) != 0) {
            discard_disabled(lane);
            return false;
        }
        if (!lanes[lane].queue.try_pop(heads[lane])) return false;
        head_valid[lane] = 1;
        return true;
    }

    bool next_envelope(Envelope& output) noexcept {
        bool any = false;
        std::size_t selected = 0;
        for (std::size_t lane = 0; lane < kLaneCount; ++lane) {
            if (!fill_head(lane)) continue;
            if (!any || heads[lane].update.receive_monotonic_ns
                           < heads[selected].update.receive_monotonic_ns) {
                selected = lane;
                any = true;
            }
        }
        if (!any) return false;
        output = heads[selected];
        head_valid[selected] = 0;
        return true;
    }
};

Feed::Feed(std::string url, std::vector<polymarket_bbo::Binding> bindings, Mode mode,
           int socket_busy_poll_us)
    : impl_(std::make_unique<Impl>(std::move(url), bindings, mode, socket_busy_poll_us)) {}
Feed::~Feed() { stop(); }

void Feed::start() {
    bool expected = false;
    if (!impl_->started.compare_exchange_strong(expected, true)) return;
    for (auto& feed : impl_->feeds) feed->start();
}

void Feed::stop() noexcept {
    if (!impl_ || !impl_->started.exchange(false)) return;
    for (auto& feed : impl_->feeds) feed->stop();
}

bool Feed::try_next_actionable(Decision& decision) noexcept {
    Envelope envelope;
    for (std::size_t drained = 0; drained < kMaxDrainPerTry; ++drained) {
        if (!impl_->next_envelope(envelope)) return false;
        auto candidate = impl_->gate.observe(envelope);
        if (candidate.outcome == Outcome::Actionable) {
            decision = candidate;
            return true;
        }
    }
    // Never let a continuously replenished public feed monopolize the single
    // decision owner. The caller can immediately call again; queued lineage is
    // preserved because no extra envelope is consumed here.
    return false;
}

std::array<std::uint64_t, kLaneCount> Feed::generations() const noexcept {
    std::array<std::uint64_t, kLaneCount> out{};
    for (std::size_t lane = 0; lane < kLaneCount; ++lane)
        out[lane] = impl_->lanes[lane].generation.load(std::memory_order_acquire);
    return out;
}

FeedSnapshot Feed::snapshot() const noexcept {
    FeedSnapshot out;
    out.started = impl_->started.load(std::memory_order_acquire) ? 1 : 0;
    out.disabled_mask = impl_->disabled_mask.load(std::memory_order_acquire);
    for (std::size_t lane = 0; lane < kLaneCount; ++lane) {
        auto& target = out.lanes[lane];
        target.generation = impl_->lanes[lane].generation.load(std::memory_order_acquire);
        target.decoded_updates = impl_->lanes[lane].decoded_updates.load(std::memory_order_relaxed);
        target.queue_drops = impl_->lanes[lane].queue_drops.load(std::memory_order_relaxed);
        target.decode_failures = impl_->lanes[lane].decode_failures.load(std::memory_order_relaxed);
        target.backlog = impl_->lanes[lane].queue.approximate_size();
        target.disabled = (out.disabled_mask & static_cast<std::uint8_t>(1U << lane)) != 0;
    }
    out.gate = impl_->gate.metrics();
    return out;
}

} // namespace pm::v7::redundant_bbo
