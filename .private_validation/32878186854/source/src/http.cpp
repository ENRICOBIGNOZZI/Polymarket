#include "pm/http.hpp"
#include <curl/curl.h>
#include <stdexcept>

namespace pm {
namespace {
size_t write_cb(char* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* out = static_cast<std::string*>(userdata);
    out->append(ptr, size * nmemb);
    return size * nmemb;
}
}

HttpClient::HttpClient() {
    static const int init = [](){ curl_global_init(CURL_GLOBAL_DEFAULT); return 1; }();
    (void)init;
}
HttpClient::~HttpClient() = default;

HttpResponse HttpClient::request(const std::string& method, const std::string& url, const std::string& body,
                                 const std::vector<std::pair<std::string,std::string>>& headers) const {
    CURL* curl = curl_easy_init();
    if (!curl) throw std::runtime_error("curl_easy_init failed");
    HttpResponse resp;
    struct curl_slist* list = nullptr;
    for (const auto& [k,v] : headers) list = curl_slist_append(list, (k + ": " + v).c_str());
    if (method == "POST") list = curl_slist_append(list, "Content-Type: application/json");

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 10L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 30L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "polymarket-quant-engine/0.2");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp.body);
    curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
    if (list) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, list);
    if (method == "POST") {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
    }

    const auto code = curl_easy_perform(curl);
    if (code != CURLE_OK) {
        std::string err = curl_easy_strerror(code);
        if (list) curl_slist_free_all(list);
        curl_easy_cleanup(curl);
        throw std::runtime_error("HTTP request failed: " + err + " url=" + url);
    }
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &resp.status);
    if (list) curl_slist_free_all(list);
    curl_easy_cleanup(curl);
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
