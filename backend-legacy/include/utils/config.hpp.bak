#pragma once

#include <string>
#include <vector>
#include <map>
#include <yaml-cpp/yaml.h>
#include <spdlog/spdlog.h>

namespace rvc {

// ──────────────────────────────────────────────────────────
// Configuration loader (mirrors Python Config class)
// ──────────────────────────────────────────────────────────
class Config {
public:
    explicit Config(const YAML::Node& root);

    static Config from_yaml(const std::string& path);
    static Config default_config(); // loads config.yaml from project root

    // Access via dot notation: e.g. "input.contract.sample_rate"
    YAML::Node get(const std::string& key) const;

    // Get a section as a map (for iterating)
    const YAML::Node& section(const std::string& key) const;

    // Typed getters with defaults
    template<typename T>
    T get_or(const std::string& key, T default_val) const;

    int get_int(const std::string& key, int default_val) const;
    double get_double(const std::string& key, double default_val) const;
    bool get_bool(const std::string& key, bool default_val) const;
    std::string get_string(const std::string& key, const std::string& default_val) const;

    const YAML::Node& root() const { return root_; }

private:
    YAML::Node root_;
    mutable std::map<std::string, YAML::Node> cache_;

    YAML::Node resolve_path(const std::string& key) const;
};

} // namespace rvc
