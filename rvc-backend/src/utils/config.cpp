#include "utils/config.hpp"
#include <cmath>
#include <stdexcept>
#include <fstream>
#include <sstream>
#include <spdlog/spdlog.h>

namespace rvc {

Config::Config(const YAML::Node& root) : root_(root) {
    if (!root_.IsMap()) {
        throw std::invalid_argument("Invalid configuration <root>=" + YAML::Dump(root_) + "; expected a mapping");
    }
}

Config Config::from_yaml(const std::string& path) {
    try {
        YAML::Node root = YAML::LoadFile(path);
        Config config(root);
        config.base_dir_ = std::filesystem::absolute(path).parent_path();
        return config;
    } catch (const YAML::Exception& e) {
        spdlog::error("Failed to load config {}: {}", path, e.what());
        throw;
    }
}

Config Config::default_config() {
    // Try to find config.yaml relative to executable or in current dir
    std::vector<std::string> candidates = {
        "config.yaml",
        "../config.yaml",
        "../../config.yaml",
    };
    for (const auto& p : candidates) {
        std::ifstream f(p);
        if (f.good()) {
            f.close();
            return from_yaml(p);
        }
    }
    spdlog::error("config.yaml not found in any candidate path");
    throw std::runtime_error("config.yaml not found");
}

YAML::Node Config::resolve_path(const std::string& key) const {
    const auto cached = cache_.find(key);
    if (cached != cache_.end()) return cached->second;
    YAML::Node node = root_;
    size_t start = 0;
    while (start < key.size()) {
        if (!node.IsMap()) {
            const std::string parent = key.substr(0, start - 1);
            throw std::invalid_argument("Invalid configuration " + parent + "=" + YAML::Dump(node)
                + "; expected a mapping for " + key);
        }
        const size_t dot = key.find('.', start);
        const std::string part = key.substr(start, dot == std::string::npos ? dot : dot - start);
        const YAML::Node current = node;
        const YAML::Node child = current[part];
        if (!child.IsDefined()) return YAML::Node(YAML::NodeType::Undefined);
        node.reset(child);
        if (dot == std::string::npos) break;
        start = dot + 1;
    }
    cache_[key] = node;
    return node;
}

YAML::Node Config::get(const std::string& key) const {
    return resolve_path(key);
}

[[noreturn]] void Config::reject_value(const std::string& key, const std::string& expected) const {
    throw std::invalid_argument("Invalid configuration " + key + "=" + YAML::Dump(resolve_path(key))
        + "; expected " + expected);
}

YAML::Node Config::scalar(const std::string& key, const std::string& type) const {
    const auto node = resolve_path(key);
    if (!node.IsDefined()) return node;
    if (!node.IsScalar()) reject_value(key, "a non-null scalar");
    const auto tag = node.Tag();
    const std::string prefix = "tag:yaml.org,2002:";
    if (!tag.empty() && tag != "?" && tag != prefix + type
        && !(type == "float" && tag == prefix + "int")
        && !(type == "str" && tag == "!")) {
        reject_value(key, "a scalar tagged as " + type);
    }
    return node;
}

int Config::get_int(const std::string& key, int default_val) const {
    const auto node = scalar(key, "int");
    if (!node.IsDefined()) return default_val;
    int value;
    if (!YAML::convert<int>::decode(node, value)) reject_value(key, "an integer");
    return value;
}

int Config::get_int(const std::string& key, int default_val, int minimum, int maximum) const {
    const int value = get_int(key, default_val);
    if (value < minimum || value > maximum) {
        reject_value(key, "an integer in [" + std::to_string(minimum) + ", " + std::to_string(maximum) + "]");
    }
    return value;
}

double Config::get_double(const std::string& key, double default_val) const {
    const auto node = scalar(key, "float");
    if (!node.IsDefined()) return default_val;
    double value;
    if (node.Tag() == "tag:yaml.org,2002:int") {
        long long integer;
        if (!YAML::convert<long long>::decode(node, integer)) reject_value(key, "an integer-tagged number");
        value = static_cast<double>(integer);
    } else if (!YAML::convert<double>::decode(node, value) || !std::isfinite(value)) {
        reject_value(key, "a finite number");
    }
    return value;
}

bool Config::get_bool(const std::string& key, bool default_val) const {
    const auto node = scalar(key, "bool");
    if (!node.IsDefined()) return default_val;
    bool value;
    if (!YAML::convert<bool>::decode(node, value)) reject_value(key, "a boolean");
    return value;
}

std::string Config::get_string(const std::string& key, const std::string& default_val) const {
    const auto node = scalar(key, "str");
    if (!node.IsDefined()) return default_val;
    if (node.Tag() != "!" && node.Tag() != "tag:yaml.org,2002:str") {
        bool boolean;
        double number;
        if (YAML::convert<bool>::decode(node, boolean) || YAML::convert<double>::decode(node, number)) {
            reject_value(key, "a string");
        }
    }
    return node.Scalar();
}

std::filesystem::path Config::resolve_file_path(const std::string& key,
                                                 const std::string& default_val, bool allow_empty) const {
    const auto value = get_string(key, default_val);
    if (value.empty()) {
        if (allow_empty) return {};
        reject_value(key, "a nonempty path");
    }
    const std::filesystem::path path(value);
    return path.is_absolute() ? path : base_dir_ / path;
}

} // namespace rvc
