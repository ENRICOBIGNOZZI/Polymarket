#pragma once
#include <memory>
#include <string>
#include <string_view>

namespace pm::v7::exact_arb_graph {
// Offline-only book replay using the SAME decoder as the native observer.
// No order, socket or authentication interface. Input files must be immutable.
class NativeWsReplay final {
public:
    NativeWsReplay(std::string_view session_manifest, std::string_view expected_model_sha);
    ~NativeWsReplay();
    NativeWsReplay(const NativeWsReplay&) = delete;
    NativeWsReplay& operator=(const NativeWsReplay&) = delete;
    [[nodiscard]] std::string ingest(std::string_view frame_record);
    [[nodiscard]] std::string receipt() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace pm::v7::exact_arb_graph
