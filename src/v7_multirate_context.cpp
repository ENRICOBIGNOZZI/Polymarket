#include "pm/v7_multirate_context.hpp"
#include <boost/json.hpp>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <stdexcept>
#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#elif defined(__APPLE__)
#include <pthread/qos.h>
#endif

namespace pm::v7 {
namespace {
std::int64_t mono_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
// The slow reader must not inherit the decision CPU affinity on London.
// Failure leaves context unavailable; it never disables independent fast risk-off.
bool isolate_slow_thread() noexcept {
#if defined(__linux__)
    const char* text = std::getenv("PM_V7_CONTROL_CPUSET");
    if (!text || !*text) return false;
    cpu_set_t set; CPU_ZERO(&set);
    const char* p = text;
    while (*p) {
        char* end = nullptr;
        const long first = std::strtol(p, &end, 10);
        if (end == p || first < 0 || first >= CPU_SETSIZE) return false;
        long last = first; p = end;
        if (*p == '-') {
            const char* begin = ++p;
            last = std::strtol(p, &end, 10);
            if (end == begin || last < first || last >= CPU_SETSIZE) return false;
            p = end;
        }
        for (long cpu = first; cpu <= last; ++cpu) CPU_SET(cpu, &set);
        if (*p == ',') ++p;
        else if (*p) return false;
    }
    return pthread_setaffinity_np(pthread_self(), sizeof(set), &set) == 0;
#elif defined(__APPLE__)
    return pthread_set_qos_class_self_np(QOS_CLASS_UTILITY, 0) == 0;
#else
    return false;
#endif
}
} // namespace

SlowContextSnapshot decode_slow_context(std::string_view bytes,
    const SlowContextIdentity& identity, std::int64_t now_ns) {
    namespace json = boost::json;
    if (bytes.empty() || bytes.size() > 65536 || now_ns <= 0)
        throw std::invalid_argument("slow context size/clock");
    const auto raw = json::parse(bytes);
    const auto& o = raw.as_object();
    const auto eq = [&](const char* key, std::string_view expected) {
        return std::string_view(o.at(key).as_string()) == expected;
    };
    if (!eq("schema", "polymarket_v7_slow_context_v1")
        || !eq("code_sha", identity.code_sha) || !eq("run_id", identity.run_id)
        || !eq("market_id", identity.market_id) || !eq("asset", identity.asset)
        || !eq("horizon", identity.horizon) || !o.at("paper_only").as_bool()
        || o.at("authenticated_execution").as_bool() || o.at("real_order_submission").as_bool()
        || !o.at("observation_only").as_bool())
        throw std::invalid_argument("slow context identity/authority");
    SlowContextSnapshot result;
    result.published_ns = json::value_to<std::int64_t>(o.at("published_monotonic_ns"));
    result.version = json::value_to<std::uint64_t>(o.at("version"));
    if (!result.version || result.published_ns <= 0 || result.published_ns > now_ns)
        throw std::invalid_argument("slow context publication clock");
    const auto& fields = o.at("fields").as_object();
    if (fields.size() != kSlowContextFields) throw std::invalid_argument("slow context field schema");
    for (std::size_t i = 0; i < kSlowContextFields; ++i) {
        const auto& item = fields.at(kSlowContextNames[i]);
        if (item.is_null()) continue;
        const auto& field = item.as_object();
        auto& out = result.fields[i];
        out.value = json::value_to<double>(field.at("value"));
        out.receive_ns = json::value_to<std::int64_t>(field.at("receive_monotonic_ns"));
        out.expires_ns = json::value_to<std::int64_t>(field.at("expires_monotonic_ns"));
        out.source_version = json::value_to<std::uint64_t>(field.at("source_version"));
        if (!std::isfinite(out.value) || out.receive_ns <= 0 || out.receive_ns > result.published_ns
            || out.expires_ns < out.receive_ns || !out.source_version)
            throw std::invalid_argument("slow context field clock/value");
        result.valid_mask |= 1U << i;
    }
    result.envelope_valid = 1;
    return result;
}

SlowContextFeed::SlowContextFeed(std::string path, SlowContextIdentity identity)
    : path_(std::move(path)), identity_(std::move(identity)) {}
SlowContextFeed::~SlowContextFeed() { stop(); }
void SlowContextFeed::start() {
    if (!path_.empty() && !thread_.joinable()) {
        stopping_.store(false);
        thread_ = std::thread([this] { run(); });
    }
}
void SlowContextFeed::stop() noexcept {
    stopping_.store(true, std::memory_order_release);
    if (thread_.joinable()) thread_.join();
}
void SlowContextFeed::run() noexcept {
    if (!isolate_slow_thread()) { failures_.fetch_add(1); return; }
    std::uint64_t last_version = 0;
    while (!stopping_.load(std::memory_order_acquire)) {
        try {
            std::ifstream input(path_, std::ios::binary | std::ios::ate);
            if (!input || input.tellg() <= 0 || input.tellg() > 65536)
                throw std::runtime_error("slow context unavailable");
            std::string bytes(static_cast<std::size_t>(input.tellg()), '\0');
            input.seekg(0); input.read(bytes.data(), static_cast<std::streamsize>(bytes.size()));
            if (!input) throw std::runtime_error("slow context partial read");
            const auto update = decode_slow_context(bytes, identity_, mono_ns());
            if (update.version > last_version) {
                (void)mailbox_.publish(update);
                last_version = update.version;
            }
        } catch (...) {
            failures_.fetch_add(1, std::memory_order_relaxed);
            SlowContextSnapshot invalid;
            invalid.published_ns = mono_ns();
            invalid.version = static_cast<std::uint64_t>(invalid.published_ns);
            (void)mailbox_.publish(invalid);
        }
        for (int i = 0; i < 4 && !stopping_.load(std::memory_order_acquire); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
}
} // namespace pm::v7
