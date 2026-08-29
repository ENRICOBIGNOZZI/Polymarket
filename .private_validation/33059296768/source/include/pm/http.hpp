#pragma once
#include <string>
#include <vector>
#include <utility>

namespace pm {
struct HttpResponse { long status = 0; std::string body; };
class HttpClient {
public:
    HttpClient();
    ~HttpClient();
    HttpResponse get(const std::string& url, const std::vector<std::pair<std::string,std::string>>& headers = {}) const;
    HttpResponse post_json(const std::string& url, const std::string& body, const std::vector<std::pair<std::string,std::string>>& headers = {}) const;
private:
    HttpResponse request(const std::string& method, const std::string& url, const std::string& body,
                         const std::vector<std::pair<std::string,std::string>>& headers) const;
};
}
