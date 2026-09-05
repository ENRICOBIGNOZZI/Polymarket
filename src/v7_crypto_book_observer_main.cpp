#include "pm/v7_crypto_book_observer.hpp"

#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
std::atomic<bool> stop_requested{false};
void signal_handler(int) noexcept { stop_requested.store(true, std::memory_order_relaxed); }

struct Options {
    std::string config = "config/paper_v7.json";
    std::string run_root = "runs/paper_v7_live";
    std::string model_sha;
    std::string ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
};

Options parse(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (++i >= argc) throw std::runtime_error("missing value after " + arg);
            return argv[i];
        };
        if (arg == "--config") out.config = next();
        else if (arg == "--run-root") out.run_root = next();
        else if (arg == "--model-sha") out.model_sha = next();
        else if (arg == "--ws-url") out.ws_url = next();
        else throw std::runtime_error("unknown argument: " + arg);
    }
    return out;
}
} // namespace

int main(int argc, char** argv) {
    try {
        std::signal(SIGINT, signal_handler);
        std::signal(SIGTERM, signal_handler);
        const auto options = parse(argc, argv);
        pm::v7::research::CryptoBookObserver observer(
            options.run_root, options.config, options.model_sha, options.ws_url);
        std::int64_t status_ticks = 0;
        while (!stop_requested.load(std::memory_order_relaxed)) {
            observer.poll();
            if (++status_ticks >= 100) {
                observer.write_status();
                status_ticks = 0;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        observer.stop();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_crypto_book_observer: " << error.what() << '\n';
        return 2;
    }
}
