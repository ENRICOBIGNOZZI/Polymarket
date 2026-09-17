#pragma once

#include "pm/v7_coinbase_l2.hpp"
#include "pm/v7_external_ingress.hpp"
#include "pm/v7_external_ws.hpp"

#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>

namespace pm::v7::external_fair {

// Canonical Coinbase Exchange level2/level2_batch observer. Both the main V7
// data plane and zero-authority flash shadow use this exact implementation so
// feed semantics cannot silently drift between validation and production.
class CoinbaseL2FrameObserver final : public ExternalFrameObserver {
public:
    CoinbaseL2FrameObserver(ExternalVenueIngress& ingress,
                            std::uint64_t asset_handle) noexcept;

    void on_connection_epoch(std::uint64_t epoch) noexcept override;
    void on_frame(std::uint64_t epoch, std::int64_t receive_ns,
                  std::int64_t wall_ns, std::string_view payload) noexcept override;

    [[nodiscard]] CoinbaseL2Metrics metrics() const noexcept;
    [[nodiscard]] std::string diagnostic() const;

private:
    void reset_epoch(std::uint64_t epoch) noexcept;
    void publish(std::int64_t receive_ns, std::int64_t wall_ns) noexcept;
    void fail(std::string diagnostic) noexcept;

    ExternalVenueIngress& ingress_;
    std::uint64_t asset_handle_ = 0;
    mutable std::mutex mutex_{};
    CoinbaseL2Book book_{};
    std::uint64_t connection_epoch_ = 0;
    std::uint64_t sequence_ = 0;
    std::uint64_t parse_failures_ = 0;
    std::string last_protocol_error_{};
    std::string last_message_type_{};
};

} // namespace pm::v7::external_fair
