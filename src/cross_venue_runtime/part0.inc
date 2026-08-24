#include "pm/cross_venue.hpp"
#include "pm/engine.hpp"

#include <boost/json.hpp>
#include <openssl/bio.h>
#include <openssl/buffer.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <unordered_set>

#if !defined(_WIN32)
#include <sys/stat.h>
#endif

namespace pm::cross {
namespace json = boost::json;
namespace {

constexpr double kEpsilon = 1e-12;

std::string trim(std::string value) {
    const auto not_space = [](unsigned char c) { return !std::isspace(c); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
}

std::string lower_ascii(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

bool bool_value(std::string value, bool fallback = false) {
    value = lower_ascii(trim(std::move(value)));
    if (value == "1" || value == "true" || value == "yes" || value == "on") return true;
    if (value == "0" || value == "false" || value == "no" || value == "off") return false;
    return fallback;
}

std::string unquote(std::string value) {
    value = trim(std::move(value));
    if (value.size() >= 2 && ((value.front() == '"' && value.back() == '"') ||
                              (value.front() == '\'' && value.back() == '\''))) {
        value = value.substr(1, value.size() - 2);
    }
    return value;
}

std::unordered_map<std::string, std::string> load_env_map(const std::filesystem::path& raw_path) {
    std::unordered_map<std::string, std::string> values;
    const auto path = expand_user_path(raw_path);
    std::ifstream input(path);
    if (!input) return values;
    std::string line;
    while (std::getline(input, line)) {
        line = trim(std::move(line));
        if (line.empty() || line.front() == '#') continue;
        if (line.rfind("export ", 0) == 0) line = trim(line.substr(7));
        const auto pos = line.find('=');
        if (pos == std::string::npos || pos == 0) continue;
        auto key = trim(line.substr(0, pos));
        auto value = unquote(line.substr(pos + 1));
        if (!key.empty()) values[std::move(key)] = std::move(value);
    }
    return values;
}

std::string first_value(
    const std::unordered_map<std::string, std::string>& values,
    std::initializer_list<const char*> keys) {
    for (const char* key : keys) {
        if (const char* env = std::getenv(key); env != nullptr && *env != '\0') return env;
        if (const auto it = values.find(key); it != values.end() && !it->second.empty()) return it->second;
    }
    return {};
}

std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::int64_t now_seconds() { return now_ms() / 1000; }

std::string iso8601_now() {
    const auto now = std::chrono::system_clock::now();
    const auto seconds = std::chrono::time_point_cast<std::chrono::seconds>(now);
    const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(now - seconds).count();
    const std::time_t tt = std::chrono::system_clock::to_time_t(now);
    std::tm tm{};
#if defined(_WIN32)
    gmtime_s(&tm, &tt);
#else
    gmtime_r(&tt, &tm);
#endif
    std::ostringstream out;
    out << std::put_time(&tm, "%Y-%m-%dT%H:%M:%S") << '.' << std::setw(3) << std::setfill('0')
        << millis << 'Z';
    return out.str();
}

std::int64_t parse_iso_time_ms(std::string value) {
    value = trim(std::move(value));
    if (value.empty()) return 0;
    try {
        std::size_t used = 0;
        const auto numeric = std::stoll(value, &used);
        if (used == value.size()) return numeric > 100000000000LL ? numeric : numeric * 1000;
    } catch (...) {
    }
    if (value.size() < 19) return 0;
    std::tm tm{};
    std::istringstream input(value.substr(0, 19));
    input >> std::get_time(&tm, "%Y-%m-%dT%H:%M:%S");
    if (input.fail()) return 0;
#if defined(_WIN32)
    const auto seconds = _mkgmtime(&tm);
#else
    const auto seconds = timegm(&tm);
#endif
    std::int64_t milliseconds = 0;
    const auto dot = value.find('.');
    if (dot != std::string::npos) {
        std::string fraction;
        for (std::size_t i = dot + 1; i < value.size() && std::isdigit(static_cast<unsigned char>(value[i])); ++i) {
            fraction.push_back(value[i]);
            if (fraction.size() == 3) break;
        }
        while (fraction.size() < 3) fraction.push_back('0');
        if (!fraction.empty()) milliseconds = std::stoll(fraction);
    }
    return static_cast<std::int64_t>(seconds) * 1000 + milliseconds;
}

std::string base64_encode(const unsigned char* data, std::size_t size) {
    if (size == 0) return {};
    std::string out(4 * ((size + 2) / 3), '\0');
    const int written = EVP_EncodeBlock(
        reinterpret_cast<unsigned char*>(out.data()), data, static_cast<int>(size));
    if (written < 0) throw std::runtime_error("base64 encoding failed");
    out.resize(static_cast<std::size_t>(written));
    return out;
}

std::vector<unsigned char> base64_decode(std::string value) {
    value.erase(std::remove_if(value.begin(), value.end(), [](unsigned char c) {
                    return std::isspace(c) != 0;
                }),
                value.end());
    if (value.empty() || value.size() % 4 != 0) throw std::runtime_error("invalid base64 secret");
    std::vector<unsigned char> out(3 * (value.size() / 4));
    const int written = EVP_DecodeBlock(out.data(),
                                        reinterpret_cast<const unsigned char*>(value.data()),
                                        static_cast<int>(value.size()));
    if (written < 0) throw std::runtime_error("invalid base64 secret");
    std::size_t padding = 0;
    if (!value.empty() && value.back() == '=') ++padding;
    if (value.size() > 1 && value[value.size() - 2] == '=') ++padding;
    out.resize(static_cast<std::size_t>(written) - padding);
    return out;
}

std::string hmac_sha256_base64(const std::vector<unsigned char>& key, std::string_view message) {
    std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
    unsigned int digest_size = 0;
    if (HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()),
             reinterpret_cast<const unsigned char*>(message.data()), message.size(), digest.data(),
             &digest_size) == nullptr) {
        throw std::runtime_error("HMAC-SHA256 failed");
    }
    return base64_encode(digest.data(), digest_size);
}

std::string rsa_pss_sha256_base64(const std::filesystem::path& key_path, std::string_view message) {
    FILE* raw = std::fopen(key_path.string().c_str(), "rb");
    if (raw == nullptr) throw std::runtime_error("cannot open Kalshi private key: " + key_path.string());
    struct FileCloser { void operator()(FILE* handle) const { if (handle != nullptr) std::fclose(handle); } };
    std::unique_ptr<FILE, FileCloser> file(raw);
    EVP_PKEY* pkey_raw = PEM_read_PrivateKey(file.get(), nullptr, nullptr, nullptr);
    if (pkey_raw == nullptr) throw std::runtime_error("invalid Kalshi private key PEM");
    std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> pkey(pkey_raw, &EVP_PKEY_free);

    EVP_MD_CTX* ctx_raw = EVP_MD_CTX_new();
    if (ctx_raw == nullptr) throw std::runtime_error("EVP_MD_CTX_new failed");
    std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> ctx(ctx_raw, &EVP_MD_CTX_free);
    EVP_PKEY_CTX* pctx = nullptr;
    if (EVP_DigestSignInit(ctx.get(), &pctx, EVP_sha256(), nullptr, pkey.get()) <= 0 ||
        EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) <= 0 ||
        EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) <= 0) {
        throw std::runtime_error("Kalshi RSA-PSS initialization failed");
    }
    if (EVP_DigestSignUpdate(ctx.get(), message.data(), message.size()) <= 0) {
        throw std::runtime_error("Kalshi RSA-PSS update failed");
    }
    std::size_t signature_size = 0;
    if (EVP_DigestSignFinal(ctx.get(), nullptr, &signature_size) <= 0) {
        throw std::runtime_error("Kalshi RSA-PSS sizing failed");
    }
    std::vector<unsigned char> signature(signature_size);
    if (EVP_DigestSignFinal(ctx.get(), signature.data(), &signature_size) <= 0) {
        throw std::runtime_error("Kalshi RSA-PSS signing failed");
    }
    signature.resize(signature_size);
    return base64_encode(signature.data(), signature.size());
}

std::string json_string(const json::value& value, std::string fallback = {}) {
    try {
        if (value.is_string()) return std::string(value.as_string());
        if (value.is_int64()) return std::to_string(value.as_int64());
        if (value.is_uint64()) return std::to_string(value.as_uint64());
        if (value.is_double()) {
            std::ostringstream out;
            out << std::setprecision(17) << value.as_double();
            return out.str();
        }
        if (value.is_bool()) return value.as_bool() ? "true" : "false";
    } catch (...) {
    }
    return fallback;
}

double json_double(const json::value& value, double fallback = 0.0) {
    try {
        if (value.is_double()) return value.as_double();
        if (value.is_int64()) return static_cast<double>(value.as_int64());
        if (value.is_uint64()) return static_cast<double>(value.as_uint64());
        if (value.is_string()) return std::stod(std::string(value.as_string()));
    } catch (...) {
    }
    return fallback;
}

std::int64_t json_int64(const json::value& value, std::int64_t fallback = 0) {
    try {
        if (value.is_int64()) return value.as_int64();
        if (value.is_uint64()) return static_cast<std::int64_t>(value.as_uint64());
        if (value.is_double()) return static_cast<std::int64_t>(value.as_double());
        if (value.is_string()) return std::stoll(std::string(value.as_string()));
    } catch (...) {
    }
    return fallback;
}

bool json_bool(const json::value& value, bool fallback = false) {
    if (value.is_bool()) return value.as_bool();
    return bool_value(json_string(value), fallback);
}

const json::value* find_value(const json::object& object, std::string_view key) {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

std::string object_string(const json::object& object, std::string_view key, std::string fallback = {}) {
    if (const auto* value = find_value(object, key)) return json_string(*value, std::move(fallback));
    return fallback;
}

double object_double(const json::object& object, std::string_view key, double fallback = 0.0) {
    if (const auto* value = find_value(object, key)) return json_double(*value, fallback);
    return fallback;
}

std::int64_t object_int64(const json::object& object, std::string_view key, std::int64_t fallback = 0) {
    if (const auto* value = find_value(object, key)) return json_int64(*value, fallback);
    return fallback;
}

bool object_bool(const json::object& object, std::string_view key, bool fallback = false) {
    if (const auto* value = find_value(object, key)) return json_bool(*value, fallback);
    return fallback;
}

std::string url_encode(std::string_view value) {
    std::ostringstream out;
    out << std::uppercase << std::hex;
    for (const unsigned char c : value) {
