#pragma once

#include <cstdint>
#include <vector>
#include <string>
#include <filesystem>

namespace rvc {

class IndexSearch {
public:
    bool load(const std::filesystem::path& index_path, uint32_t feature_dim = 768);
    bool loaded() const { return !centroids_.empty(); }

    std::vector<float> search(const std::vector<float>& features,
                              uint32_t feat_dim, float index_rate);

private:
    uint32_t feature_dim_ = 768;
    uint32_t nlist_ = 0;
    std::vector<std::vector<float>> centroids_;
    std::vector<std::vector<std::vector<float>>> inverted_lists_;

    bool parse_ivf_index(const std::filesystem::path& path);
    uint32_t find_nearest_centroid(const std::vector<float>& vec) const;
};

} // namespace rvc
