#pragma once

#include <filesystem>
#include <memory>
#include <string>

namespace pm::v7::research {

// Zero-authority BTC M5 CLOB book evidence. Public market data only; never
// owns orders, inventory, capital, signatures or authenticated execution.
class CryptoBookObserver final {
public:
    CryptoBookObserver(std::filesystem::path run_root,
                       std::filesystem::path config_path,
                       std::string model_sha,
                       std::string ws_url);
    ~CryptoBookObserver();

    CryptoBookObserver(const CryptoBookObserver&) = delete;
    CryptoBookObserver& operator=(const CryptoBookObserver&) = delete;

    void poll() noexcept;
    void write_status(bool stopped = false) noexcept;
    void stop() noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::v7::research
