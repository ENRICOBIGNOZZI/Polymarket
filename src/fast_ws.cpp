#include "pm/fast_ws.hpp"

#include <boost/json.hpp>
#include <curl/curl.h>
#include <curl/websockets.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <mutex>
#include <poll.h>
#include <stdexcept>
#include <thread>
#include <utility>

namespace pm::fast {
namespace {
namespace json = boost::json;

std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

void ensure_curl_global() {
    static const int initialized = [] {
        const auto rc = curl_global_init(CURL_GLOBAL_DEFAULT);
        if (rc != CURLE_OK) throw std::runtime_error("curl_global_init failed");
        return 1;
    }();
    (void)initialized;
}

std::string subscription(const std::vector<std::string>& ids) {
    json::array assets;
    assets.reserve(ids.size());
    for (const auto& id : ids) assets.emplace_back(id);
    return json::serialize(json::object{
        {"assets_ids", std::move(assets)},
        {"type", "market"},
        {"custom_feature_enabled", true},
    });
}

bool send_text(CURL* curl, std::string_view text) {
    for (int attempt = 0; attempt < 50; ++attempt) {
        std::size_t sent = 0;
        const auto rc = curl_ws_send(curl, text.data(), text.size(), &sent,
                                     static_cast<curl_off_t>(text.size()), CURLWS_TEXT);
        if (rc == CURLE_AGAIN) {
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            continue;
        }
        return rc == CURLE_OK && sent == text.size();
    }
    return false;
}

} // namespace

struct MarketWebSocketFeed::Impl {
    std::string url;
    std::vector<std::vector<std::string>> shards;
    MessageHandler on_message;
    ErrorHandler on_error;
    std::vector<std::jthread> threads;
    std::atomic<bool> started{false};
    std::atomic<std::size_t> connected{0};
    std::atomic<std::uint64_t> messages{0};
    std::atomic<std::uint64_t> reconnects{0};
    std::atomic<std::uint64_t> errors{0};

    Impl(std::string endpoint, std::vector<std::string> ids, std::size_t shard_size,
         MessageHandler message_handler, ErrorHandler error_handler)
        : url(std::move(endpoint)), on_message(std::move(message_handler)),
          on_error(std::move(error_handler)) {
        ensure_curl_global();
        ids.erase(std::remove_if(ids.begin(), ids.end(), [](const std::string& id) {
            return id.empty();
        }), ids.end());
        std::sort(ids.begin(), ids.end());
        ids.erase(std::unique(ids.begin(), ids.end()), ids.end());
        const std::size_t width = std::max<std::size_t>(1, shard_size);
        for (std::size_t pos = 0; pos < ids.size(); pos += width) {
            const auto end = std::min(ids.size(), pos + width);
            shards.emplace_back(ids.begin() + static_cast<std::ptrdiff_t>(pos),
                                ids.begin() + static_cast<std::ptrdiff_t>(end));
        }
    }

    void report(std::size_t shard, std::string_view message) {
        errors.fetch_add(1, std::memory_order_relaxed);
        if (on_error) on_error(shard, message);
    }

    void run_worker(std::stop_token stop, std::size_t shard_index,
                    const std::vector<std::string>& ids) {
        int backoff_seconds = 1;
        bool first_attempt = true;
        while (!stop.stop_requested()) {
            if (!first_attempt) reconnects.fetch_add(1, std::memory_order_relaxed);
            first_attempt = false;

            CURL* curl = curl_easy_init();
            if (curl == nullptr) {
                report(shard_index, "curl_easy_init failed");
                std::this_thread::sleep_for(std::chrono::seconds(backoff_seconds));
                backoff_seconds = std::min(30, backoff_seconds * 2);
                continue;
            }
            curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
            curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L);
            curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 10000L);
            curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 15000L);
            curl_easy_setopt(curl, CURLOPT_USERAGENT, "polymarket-fast-arb-shadow/0.9");
            curl_easy_setopt(curl, CURLOPT_TCP_KEEPALIVE, 1L);
            curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
            curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);

            const auto connect_rc = curl_easy_perform(curl);
            if (connect_rc != CURLE_OK) {
                report(shard_index, curl_easy_strerror(connect_rc));
                curl_easy_cleanup(curl);
                std::this_thread::sleep_for(std::chrono::seconds(backoff_seconds));
                backoff_seconds = std::min(30, backoff_seconds * 2);
                continue;
            }
            if (!send_text(curl, subscription(ids))) {
                report(shard_index, "market subscription send failed");
                curl_easy_cleanup(curl);
                std::this_thread::sleep_for(std::chrono::seconds(backoff_seconds));
                backoff_seconds = std::min(30, backoff_seconds * 2);
                continue;
            }

            connected.fetch_add(1, std::memory_order_relaxed);
            backoff_seconds = 1;
            bool connection_ok = true;
            std::string frame;
            std::int64_t last_activity = now_ms();
            std::int64_t last_ping = 0;
            curl_socket_t socket_fd = CURL_SOCKET_BAD;
            curl_easy_getinfo(curl, CURLINFO_ACTIVESOCKET, &socket_fd);

            while (!stop.stop_requested() && connection_ok) {
                const auto now = now_ms();
                if (now - last_ping >= 10000) {
                    if (!send_text(curl, "PING")) {
                        connection_ok = false;
                        break;
                    }
                    last_ping = now;
                }
                if (now - last_activity > 45000) {
                    report(shard_index, "stale websocket feed");
                    connection_ok = false;
                    break;
                }

                if (socket_fd != CURL_SOCKET_BAD) {
                    pollfd descriptor{socket_fd, POLLIN, 0};
                    (void)::poll(&descriptor, 1, 250);
                } else {
                    std::this_thread::sleep_for(std::chrono::milliseconds(25));
                }

                for (;;) {
                    std::array<char, 65536> buffer{};
                    std::size_t received = 0;
                    const curl_ws_frame* metadata = nullptr;
                    const auto rc = curl_ws_recv(curl, buffer.data(), buffer.size(),
                                                 &received, &metadata);
                    if (rc == CURLE_AGAIN) break;
                    if (rc != CURLE_OK) {
                        report(shard_index, curl_easy_strerror(rc));
                        connection_ok = false;
                        break;
                    }
                    last_activity = now_ms();
                    if (metadata != nullptr && (metadata->flags & CURLWS_CLOSE)) {
                        connection_ok = false;
                        break;
                    }
                    if (received == 0 || metadata == nullptr) continue;
                    if (metadata->flags & (CURLWS_TEXT | CURLWS_CONT)) {
                        frame.append(buffer.data(), received);
                        if (metadata->bytesleft == 0) {
                            if (frame != "PONG" && !frame.empty()) {
                                messages.fetch_add(1, std::memory_order_relaxed);
                                on_message(frame, last_activity, shard_index);
                            }
                            frame.clear();
                        }
                    }
                }
            }

            connected.fetch_sub(1, std::memory_order_relaxed);
            curl_easy_cleanup(curl);
            if (!stop.stop_requested()) {
                std::this_thread::sleep_for(std::chrono::seconds(backoff_seconds));
                backoff_seconds = std::min(30, backoff_seconds * 2);
            }
        }
    }

    void start() {
        bool expected = false;
        if (!started.compare_exchange_strong(expected, true)) return;
        threads.reserve(shards.size());
        for (std::size_t i = 0; i < shards.size(); ++i) {
            threads.emplace_back([this, i, ids = shards[i]](std::stop_token stop) {
                run_worker(stop, i, ids);
            });
        }
    }

    void stop() {
        if (!started.exchange(false)) return;
        for (auto& thread : threads) thread.request_stop();
        threads.clear();
    }
};

MarketWebSocketFeed::MarketWebSocketFeed(std::string url,
                                         std::vector<std::string> asset_ids,
                                         std::size_t shard_size,
                                         MessageHandler on_message,
                                         ErrorHandler on_error)
    : impl_(std::make_unique<Impl>(std::move(url), std::move(asset_ids), shard_size,
                                   std::move(on_message), std::move(on_error))) {}

MarketWebSocketFeed::~MarketWebSocketFeed() { stop(); }

void MarketWebSocketFeed::start() { impl_->start(); }
void MarketWebSocketFeed::stop() { impl_->stop(); }

FeedSnapshot MarketWebSocketFeed::snapshot() const {
    return FeedSnapshot{
        impl_->shards.size(),
        impl_->connected.load(std::memory_order_relaxed),
        impl_->messages.load(std::memory_order_relaxed),
        impl_->reconnects.load(std::memory_order_relaxed),
        impl_->errors.load(std::memory_order_relaxed),
    };
}

} // namespace pm::fast
