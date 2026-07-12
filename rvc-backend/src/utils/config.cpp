#include "utils/config.hpp"
#include <fstream>
#include <sstream>
#include <spdlog/spdlog.h>

namespace rvc {

Config::Config(const YAML::Node& root) : root_(root) {}

Config Config::from_yaml(const std::string& path) {
    try {
        YAML::Node root = YAML::LoadFile(path);
        return Config(root);
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
    auto it = cache_.find(key);
    if (it != cache_.end()) {
        return it->second;
    }

    YAML::Node node = root_;
    size_t start = 0;
    while (start < key.size()) {
        size_t dot = key.find('.', start);
        std::string part;
        if (dot == std::string::npos) {
            part = key.substr(start);
            start = key.size();
        } else {
            part = key.substr(start, dot - start);
            start = dot + 1;
        }
        if (node.IsMap() && node[part]) {
            node = node[part];
        } else {
            return YAML::Node(); // null
        }
    }
    cache_[key] = node;
    return node;
}

YAML::Node Config::get(const std::string& key) const {
    return resolve_path(key);
}

const YAML::Node& Config::section(const std::string& key) const {
    static YAML::Node empty;
    if (root_[key]) {
        return root_[key];
    }
    return empty;
}

int Config::get_int(const std::string& key, int default_val) const {
    YAML::Node n = resolve_path(key);
    if (n && n.IsScalar()) {
        try {
            return n.as<int>();
        } catch (...) {}
    }
    return default_val;
}

double Config::get_double(const std::string& key, double default_val) const {
    YAML::Node n = resolve_path(key);
    if (n && n.IsScalar()) {
        try {
            return n.as<double>();
        } catch (...) {}
    }
    return default_val;
}

bool Config::get_bool(const std::string& key, bool default_val) const {
    YAML::Node n = resolve_path(key);
    if (n && n.IsScalar()) {
        try {
            return n.as<bool>();
        } catch (...) {}
    }
    return default_val;
}

std::string Config::get_string(const std::string& key, const std::string& default_val) const {
    YAML::Node n = resolve_path(key);
    if (n && n.IsScalar()) {
        try {
            return n.as<std::string>();
        } catch (...) {}
    }
    return default_val;
}

} // namespace rvc
