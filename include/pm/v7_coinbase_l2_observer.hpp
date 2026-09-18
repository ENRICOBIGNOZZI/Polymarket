#pragma once

#include "pm/v7_coinbase_l2.hpp"
#include "pm/v7_external_ingress.hpp"
#include "pm/v7_external_ws.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace boost::json { class static_resource; class parser; }

namespace pm::v7::external_fair {

inline constexpr std::size_t kCoinbaseJsonArenaBytes = 16U * 1024U * 1024U;
inline constexpr std::size_t kCoinbaseMaxChangesPerFrame = 65'536;
inline constexpr std::size_t kCoinbaseParserScratchBytes = 4U * 1024U * 1024U;

// Canonical Coinbase Exchange level2/level2_batch observer. All per-frame JSON
// and L2 scratch memory is reserved at cold start. Runtime overflow fails closed
// instead of allocating or truncating source state.
class CoinbaseL2FrameObserver final : public ExternalFrameObserver {
public:
    CoinbaseL2FrameObserver(ExternalVenueIngress& ingress,
                            std::uint64_t asset_handle);
    ~CoinbaseL2FrameObserver();

    CoinbaseL2FrameObserver(const CoinbaseL2FrameObserver&) = delete;
    CoinbaseL2FrameObserver& operator=(const CoinbaseL2FrameObserver&) = delete;

    void on_connection_epoch(std::uint64_t epoch) noexcept override;
    void on_frame(std::uint64_t epoch, std::int64_t receive_ns,
                  std::int64_t wall_ns, std::string_view payload) noexcept override;

    [[nodiscard]] CoinbaseL2Metrics metrics() const noexcept;
    [[nodiscard]] std::string diagnostic() const;

private:
    void reset_epoch(std::uint64_t epoch) noexcept;
    void publish(std::int64_t receive_ns, std::int64_t wall_ns) noexcept;
    void fail(std::string_view diagnostic) noexcept;
    void publish_telemetry(const CoinbaseL2Metrics& metrics) noexcept;
    void set_last_type(std::string_view value) noexcept;
    void set_protocol_error(std::string_view value) noexcept;

    ExternalVenueIngress& ingress_;
    std::uint64_t asset_handle_ = 0;
    CoinbaseL2Book book_{};
    std::vector<unsigned char> json_arena_;
    std::unique_ptr<boost::json::static_resource> json_resource_;
    std::vector<unsigned char> parser_scratch_;
    std::unique_ptr<boost::json::parser> parser_;
    std::unique_ptr<std::array<CoinbaseDepthLevel, kCoinbaseL2MaxLevelsPerSide>> bid_scratch_;
    std::unique_ptr<std::array<CoinbaseDepthLevel, kCoinbaseL2MaxLevelsPerSide>> ask_scratch_;
    std::unique_ptr<std::array<CoinbaseDepthChange, kCoinbaseMaxChangesPerFrame>> change_scratch_;
    std::uint64_t connection_epoch_ = 0;
    std::uint64_t sequence_ = 0;
    std::uint64_t parse_failures_ = 0;

    // Single-writer feed state is exported to the cold telemetry reader using
    // atomic fields plus a version fence. The hot callback never takes a mutex.
    std::atomic<std::uint64_t> telemetry_version_{0};
    std::atomic<std::uint64_t> published_update_count_{0};
    std::atomic<std::uint64_t> published_parse_failures_{0};
    std::atomic<std::int64_t> published_latest_receive_ns_{0};
    std::atomic<double> published_best_bid_{0.0};
    std::atomic<double> published_best_ask_{0.0};
    std::atomic<double> published_mid_{0.0};
    std::atomic<double> published_microprice_{0.0};
    std::atomic<double> published_spread_bps_{0.0};
    std::atomic<double> published_bid_l1_{0.0};
    std::atomic<double> published_ask_l1_{0.0};
    std::atomic<double> published_bid_l5_{0.0};
    std::atomic<double> published_ask_l5_{0.0};
    std::atomic<double> published_bid_l10_{0.0};
    std::atomic<double> published_ask_l10_{0.0};
    std::atomic<double> published_bid_l20_{0.0};
    std::atomic<double> published_ask_l20_{0.0};
    std::atomic<double> published_imbalance_l1_{0.0};
    std::atomic<double> published_imbalance_l5_{0.0};
    std::atomic<double> published_imbalance_l10_{0.0};
    std::atomic<std::uint32_t> published_bid_levels_{0};
    std::atomic<std::uint32_t> published_ask_levels_{0};
    std::atomic<std::uint8_t> published_state_{static_cast<std::uint8_t>(CoinbaseL2State::AwaitingSnapshot)};
    std::atomic<std::uint8_t> published_valid_{0};
    std::atomic<std::uint8_t> last_message_type_code_{0};
    std::atomic<std::uint8_t> last_protocol_error_code_{0};
};

} // namespace pm::v7::external_fair
