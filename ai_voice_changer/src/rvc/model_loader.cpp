// rvc/model_loader.cpp
#include "rvc/model_loader.hpp"
#include "utils/logging.hpp"

#include <fstream>
#include <sstream>
#include <string>

namespace mozart {

namespace {

// Find "key": <int> in a JSON-ish file. Returns matching value or default.
int find_int(const std::string& s, std::string_view key, int def) {
    auto k = "\"" + std::string(key) + "\"";
    auto pos = s.find(k);
    if (pos == std::string::npos) return def;
    pos = s.find(':', pos);
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < s.size() && (s[pos] == ' ' || s[pos] == '\t' || s[pos] == '\n' ||
                              s[pos] == '\r'))
        ++pos;
    try {
        return std::stoi(std::string(s.substr(pos)));
    } catch (...) {
        return def;
    }
}

// Find "key": <bool> in a JSON-ish file.
bool find_bool(const std::string& s, std::string_view key, bool def) {
    auto k = "\"" + std::string(key) + "\"";
    auto pos = s.find(k);
    if (pos == std::string::npos) return def;
    pos = s.find(':', pos);
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < s.size() && (s[pos] == ' ' || s[pos] == '\t' || s[pos] == '\n' ||
                              s[pos] == '\r'))
        ++pos;
    if (s.compare(pos, 4, "true") == 0) return true;
    if (s.compare(pos, 5, "false") == 0) return false;
    return def;
}

bool read_config_json(const std::filesystem::path& p, RVCModelInfo& out) {
    std::ifstream f(p);
    if (!f) return false;
    std::stringstream ss;
    ss << f.rdbuf();
    std::string s = ss.str();

    // Some RVC configs nest fields under "data"; search top-level then nested.
out.sample_rate  = find_int(s,  "sampling_rate", 40000);
    if (auto sr = find_int(s, "sampling_rate", 0); sr != 0) out.sample_rate = sr;
    out.emb_channels = find_int(s,  "emb_channels", 768);
    out.spk_id       = find_int(s,  "spk",          0);
    out.f0           = find_bool(s, "f0",           true);
    return true;
}

}  // namespace

ModelManager::ModelManager(std::filesystem::path models_dir)
    : models_dir_(std::move(models_dir)) {
    std::filesystem::create_directories(models_dir_);
}

std::vector<RVCModelInfo> ModelManager::list_models() const {
    std::vector<RVCModelInfo> result;
    if (!std::filesystem::exists(models_dir_)) return result;

    for (auto& e : std::filesystem::directory_iterator(models_dir_)) {
        if (!e.is_directory()) continue;
        RVCModelInfo info;
        info.id             = e.path().filename().string();
        info.generator_path = e.path() / (info.id + ".onnx");
        info.loaded         = std::filesystem::exists(info.generator_path);
        result.push_back(std::move(info));
    }
    return result;
}

bool ModelManager::get_model(const std::string& id, RVCModelInfo& out) const {
    out = RVCModelInfo{};
    auto dir = models_dir_ / id;
    if (!std::filesystem::is_directory(dir)) return false;
    out.id             = id;
    out.generator_path = dir / (id + ".onnx");
    if (!std::filesystem::exists(out.generator_path)) return false;
    read_config_json(dir / "config.json", out);
    out.loaded = true;
    return true;
}

bool ModelManager::activate(const std::string& id) {
    RVCModelInfo info;
    if (!get_model(id, info)) return false;
    current_ = id;
    MOZART_INFO("ModelManager: activated '%s'", id.c_str());
    return true;
}

}  // namespace mozart