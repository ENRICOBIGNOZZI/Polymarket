#include "poly/engine.hpp"

#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

namespace poly {
namespace {

std::string slurp(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::ostringstream ss; ss << f.rdbuf(); return ss.str();
}

void send_all(int fd, const std::string& data) {
    std::size_t sent = 0;
    while (sent < data.size()) {
        const ssize_t n = ::send(fd, data.data() + sent, data.size() - sent, 0);
        if (n <= 0) return;
        sent += static_cast<std::size_t>(n);
    }
}

void http_reply(int fd, const std::string& body, const std::string& content_type, int code = 200) {
    std::ostringstream h;
    h << "HTTP/1.1 " << code << (code == 200 ? " OK" : " Error") << "\r\n"
      << "Content-Type: " << content_type << "\r\n"
      << "Content-Length: " << body.size() << "\r\n"
      << "Cache-Control: no-store\r\n"
      << "Connection: close\r\n\r\n";
    send_all(fd, h.str());
    send_all(fd, body);
}

} // namespace

void serve_dashboard(QuantEngine& engine, int port, const std::string& web_root) {
    const int server_fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) throw std::runtime_error("socket failed");
    int yes = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::bind(server_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        ::close(server_fd);
        throw std::runtime_error("bind failed on port " + std::to_string(port));
    }
    if (::listen(server_fd, 64) < 0) {
        ::close(server_fd);
        throw std::runtime_error("listen failed");
    }
    std::cout << "Dashboard: http://127.0.0.1:" << port << "\n";

    while (true) {
        const int client = ::accept(server_fd, nullptr, nullptr);
        if (client < 0) continue;
        char buf[4096];
        const ssize_t n = ::recv(client, buf, sizeof(buf) - 1, 0);
        if (n <= 0) { ::close(client); continue; }
        buf[n] = '\0';
        std::istringstream req(std::string(buf, static_cast<std::size_t>(n)));
        std::string method, path, proto;
        req >> method >> path >> proto;

        if (method != "GET") {
            http_reply(client, "{\"error\":\"GET only\"}", "application/json", 405);
        } else if (path == "/api/state") {
            http_reply(client, engine.state_json(), "application/json");
        } else if (path == "/api/signals") {
            http_reply(client, engine.signals_json(), "application/json");
        } else if (path == "/api/positions") {
            http_reply(client, engine.positions_json(), "application/json");
        } else if (path == "/api/equity") {
            http_reply(client, engine.equity_json(), "application/json");
        } else if (path == "/health") {
            http_reply(client, "{\"ok\":true}", "application/json");
        } else if (path == "/" || path == "/index.html") {
            std::string html = slurp(web_root + "/index.html");
            if (html.empty()) http_reply(client, "dashboard missing", "text/plain", 404);
            else http_reply(client, html, "text/html; charset=utf-8");
        } else {
            http_reply(client, "not found", "text/plain", 404);
        }
        ::close(client);
    }
}

} // namespace poly
