#pragma once
#include <algorithm>
#include <cmath>
#include <numeric>
#include <string>
#include <cctype>
#include <vector>

namespace poly {

inline double clamp_prob(double p) {
    return std::clamp(p, 1e-6, 1.0 - 1e-6);
}
inline double logit(double p) {
    p = clamp_prob(p);
    return std::log(p / (1.0 - p));
}
inline double logistic(double x) {
    if (x >= 0.0) {
        const double e = std::exp(-x);
        return 1.0 / (1.0 + e);
    }
    const double e = std::exp(x);
    return e / (1.0 + e);
}
inline double median(std::vector<double> x) {
    if (x.empty()) return 0.0;
    const auto n = x.size();
    std::nth_element(x.begin(), x.begin() + n / 2, x.end());
    double m = x[n / 2];
    if (n % 2 == 0) {
        const auto it = std::max_element(x.begin(), x.begin() + n / 2);
        m = 0.5 * (m + *it);
    }
    return m;
}
inline double mad(const std::vector<double>& x) {
    if (x.empty()) return 0.0;
    const double m = median(x);
    std::vector<double> d; d.reserve(x.size());
    for (double v : x) d.push_back(std::abs(v - m));
    return 1.4826 * median(std::move(d));
}
inline double fee_rate_for_category(std::string c) {
    std::transform(c.begin(), c.end(), c.begin(), [](unsigned char ch){ return static_cast<char>(std::tolower(ch)); });
    if (c.find("crypto") != std::string::npos) return 0.07;
    if (c.find("sport") != std::string::npos) return 0.05;
    if (c.find("finance") != std::string::npos) return 0.04;
    if (c.find("polit") != std::string::npos) return 0.04;
    if (c.find("economic") != std::string::npos) return 0.05;
    if (c.find("culture") != std::string::npos) return 0.05;
    if (c.find("weather") != std::string::npos) return 0.05;
    if (c.find("mention") != std::string::npos) return 0.04;
    if (c.find("tech") != std::string::npos) return 0.04;
    if (c.find("geo") != std::string::npos || c.find("world") != std::string::npos) return 0.0;
    return 0.05;
}
inline double taker_fee_usdc(double shares, double price, const std::string& category) {
    const double p = clamp_prob(price);
    return shares * fee_rate_for_category(category) * p * (1.0 - p);
}

} // namespace poly
