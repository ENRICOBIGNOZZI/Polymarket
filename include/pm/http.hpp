#pragma once
#include <cstdint>
#include <memory>
#include <string>
#include <vector>
#include <utility>

namespace pm {
struct HttpTimings {
    std::int64_t dns_ns = 0;
    std::int64_t tcp_connect_ns = 0;
    std::int64_t tls_connect_ns = 0;
    std::int64_t pretransfer_ns = 0;
    std::int64_t first_byte_ns = 0;
    std::int64_t total_ns = 0;
    long new_connections = 0;
    bool connection_reused = false;
    std::string primary_ip;
};

struct HttpResponse {
    long status = 0;
    std::string body;
    HttpTimings timings{};
};

class HttpClient {
public:
    HttpClient();
    ~HttpClient();

    HttpClient(const HttpClient&) = delete;
    HttpClient& operator=(const HttpClient&) = delete;
    HttpClient(HttpClient&&) = delete;
    HttpClient& operator=(HttpClient&&) = delete;

    HttpResponse get(const std::string& url, const std::vector<std::pair<std::string,std::string>>& headers = {}) const;
    HttpResponse post_json(const std::string& url, const std::string& body, const std::vector<std::pair<std::string,std::string>>& headers = {}) const;
private:
    struct Impl;
    HttpResponse request(const std::string& method, const std::string& url, const std::string& body,
                         const std::vector<std::pair<std::string,std::string>>& headers) const;
    std::unique_ptr<Impl> impl_;
};
}
