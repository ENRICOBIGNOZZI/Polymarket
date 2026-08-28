#include "pm/http.hpp"
#include <curl/curl.h>

#include <algorithm>
#include <cstdlib>
#include <mutex>
#include <stdexcept>
#include <string_view>

namespace pm {
namespace {
size_t write_cb(char* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* out = static_cast<std::string*>(userdata);
    out->append(ptr, size * nmemb);
    return size * nmemb;
}

[[nodiscard]] bool is_public_polymarket_https(std::string_view url) noexcept {
    constexpr std::string_view scheme = "https://";
    if (!url.starts_with(scheme)) return false;
    url.remove_prefix(scheme.size());
    const auto end = url.find_first_of("/:?#");
    const auto host = url.substr(0, end);
    constexpr std::string_view suffix = ".polymarket.com";
    return host == "polymarket.com" ||
           (host.size() > suffix.size() && host.ends_with(suffix));
}

[[nodiscard]] const char* v7_public_proxy(std::string_view url) noexcept {
    if (!is_public_polymarket_https(url)) return nullptr;
    const char* proxy = std::getenv("PM_V7_HTTPS_PROXY");
    return proxy != nullptr && *proxy != '\0' ? proxy : nullptr;
}

[[nodiscard]] std::int64_t timing_ns(CURL* curl, CURLINFO field) noexcept {
    curl_off_t microseconds = 0;
    if (curl_easy_getinfo(curl, field, &microseconds) != CURLE_OK || microseconds < 0) return 0;
    return static_cast<std::int64_t>(microseconds) * 1'000LL;
}
}

struct HttpClient::Impl {
    CURL* easy = nullptr;
    mutable std::mutex mutex;

    Impl() : easy(curl_easy_init()) {
        if (easy == nullptr) throw std::runtime_error("curl_easy_init failed");
    }

    ~Impl() {
        if (easy != nullptr) curl_easy_cleanup(easy);
    }
};

HttpClient::HttpClient() {
    static const int init = [](){ curl_global_init(CURL_GLOBAL_DEFAULT); return 1; }();
    (void)init;
    impl_ = std::make_unique<Impl>();
}
HttpClient::~HttpClient() = default;

HttpResponse HttpClient::request(const std::string& method, const std::string& url, const std::string& body,
                                 const std::vector<std::pair<std::string,std::string>>& headers) const {
    // A client owns one easy handle for its whole lifetime. curl_easy_reset
    // clears request options while deliberately retaining the connection,
    // DNS and TLS session caches. The mutex is not on the market-data/decision
    // path; the dedicated I/O owner serializes transport calls.
    std::lock_guard<std::mutex> lock(impl_->mutex);
    CURL* curl = impl_->easy;
    curl_easy_reset(curl);

    HttpResponse resp;
    resp.body.reserve(4096);
    struct curl_slist* list = nullptr;
    for (const auto& [k,v] : headers) list = curl_slist_append(list, (k + ": " + v).c_str());
    if (method == "POST") list = curl_slist_append(list, "Content-Type: application/json");

    char error_buffer[CURL_ERROR_SIZE]{};

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 10L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 30L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "polymarket-quant-engine/0.2");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp.body);
    curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
    curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, error_buffer);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_FRESH_CONNECT, 0L);
    curl_easy_setopt(curl, CURLOPT_FORBID_REUSE, 0L);
    curl_easy_setopt(curl, CURLOPT_TCP_KEEPALIVE, 1L);
    curl_easy_setopt(curl, CURLOPT_TCP_KEEPIDLE, 30L);
    curl_easy_setopt(curl, CURLOPT_TCP_KEEPINTVL, 10L);
    curl_easy_setopt(curl, CURLOPT_DNS_CACHE_TIMEOUT, 300L);
    curl_easy_setopt(curl, CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_2TLS);

    // libcurl's environment-proxy behaviour differs across builds/platforms.
    // V7 therefore supplies its PAPER public-data tunnel explicitly. The
    // tunnel is loopback-only and only selected for HTTPS Polymarket hosts;
    // TLS/SNI/certificate verification remain end-to-end inside libcurl.
    if (const char* proxy = v7_public_proxy(url); proxy != nullptr) {
        curl_easy_setopt(curl, CURLOPT_PROXY, proxy);
        curl_easy_setopt(curl, CURLOPT_PROXYTYPE, CURLPROXY_HTTP);
        curl_easy_setopt(curl, CURLOPT_NOPROXY, "127.0.0.1,localhost");
    }

    if (list) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, list);
    if (method == "POST") {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
    }

    const auto code = curl_easy_perform(curl);
    if (code != CURLE_OK) {
        std::string err = error_buffer[0] != '\0' ? error_buffer : curl_easy_strerror(code);
        if (list) curl_slist_free_all(list);
        throw std::runtime_error("HTTP request failed: " + err + " url=" + url);
    }
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &resp.status);
    const auto lookup_at = timing_ns(curl, CURLINFO_NAMELOOKUP_TIME_T);
    const auto connect_at = timing_ns(curl, CURLINFO_CONNECT_TIME_T);
    const auto tls_at = timing_ns(curl, CURLINFO_APPCONNECT_TIME_T);
    const auto pretransfer_at = timing_ns(curl, CURLINFO_PRETRANSFER_TIME_T);
    const auto first_byte_at = timing_ns(curl, CURLINFO_STARTTRANSFER_TIME_T);
    resp.timings.dns_ns = lookup_at;
    resp.timings.tcp_connect_ns = std::max<std::int64_t>(0, connect_at - lookup_at);
    resp.timings.tls_connect_ns = std::max<std::int64_t>(0, tls_at - connect_at);
    resp.timings.pretransfer_ns = std::max<std::int64_t>(0, pretransfer_at - tls_at);
    resp.timings.first_byte_ns = std::max<std::int64_t>(0, first_byte_at - pretransfer_at);
    resp.timings.total_ns = timing_ns(curl, CURLINFO_TOTAL_TIME_T);
    curl_easy_getinfo(curl, CURLINFO_NUM_CONNECTS, &resp.timings.new_connections);
    resp.timings.connection_reused = resp.timings.new_connections == 0;
    char* primary_ip = nullptr;
    if (curl_easy_getinfo(curl, CURLINFO_PRIMARY_IP, &primary_ip) == CURLE_OK
        && primary_ip != nullptr) {
        resp.timings.primary_ip = primary_ip;
    }
    if (list) curl_slist_free_all(list);
    return resp;
}

HttpResponse HttpClient::get(const std::string& url, const std::vector<std::pair<std::string,std::string>>& headers) const {
    return request("GET", url, "", headers);
}
HttpResponse HttpClient::post_json(const std::string& url, const std::string& body,
                                   const std::vector<std::pair<std::string,std::string>>& headers) const {
    return request("POST", url, body, headers);
}
}
