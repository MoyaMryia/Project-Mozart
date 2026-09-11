#include "rvc/index_search.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <cmath>
#include <cstring>
#include <algorithm>
#include <limits>

namespace rvc {

bool IndexSearch::load(const std::filesystem::path& index_path, uint32_t feature_dim) {
    nlist_ = 0;
    centroids_.clear();
    inverted_lists_.clear();
    feature_dim_ = feature_dim;

    if (!std::filesystem::exists(index_path)) {
        spdlog::warn("Index file not found: {}", index_path.string());
        return false;
    }

    try {
        if (!parse_ivf_index(index_path)) {
            return false;
        }
        spdlog::info("Index loaded: {} (nlist={}, dim={})",
            index_path.filename().string(), nlist_, feature_dim_);
        return true;
    } catch (const std::exception& e) {
        nlist_ = 0;
        centroids_.clear();
        inverted_lists_.clear();
        spdlog::error("Failed to load index {}: {}", index_path.string(), e.what());
        return false;
    }
}

bool IndexSearch::parse_ivf_index(const std::filesystem::path& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;

    const auto truncated = [&](const char* what) {
        spdlog::error("FAISS IVF index is truncated or corrupt at {}", what);
        return false;
    };

    char magic[4];
    if (!f.read(magic, 4) || std::memcmp(magic, "IwFl", 4) != 0) {
        spdlog::error("Not a FAISS IVF index (bad magic)");
        return false;
    }

    uint32_t d = 0, nlist_32 = 0;
    if (!f.read(reinterpret_cast<char*>(&d), 4)
        || !f.read(reinterpret_cast<char*>(&nlist_32), 4)) {
        return truncated("header");
    }
    if (d == 0 || d > 65536 || nlist_32 == 0 || nlist_32 > 1000000) {
        spdlog::error("FAISS IVF index has invalid header: d={}, nlist={}", d, nlist_32);
        return false;
    }
    nlist_ = nlist_32;

    int64_t code_size = 0;
    if (!f.read(reinterpret_cast<char*>(&code_size), 8) || code_size < 0) {
        return truncated("code_size");
    }

    spdlog::info("FAISS IVF index: d={}, nlist={}", d, nlist_);

    if (d != feature_dim_) {
        spdlog::warn("Index dim {} != expected {}, adjusting", d, feature_dim_);
        feature_dim_ = d;
    }

    centroids_.resize(nlist_);
    for (uint32_t i = 0; i < nlist_; ++i) {
        centroids_[i].resize(feature_dim_);
        if (!f.read(reinterpret_cast<char*>(centroids_[i].data()),
                    static_cast<std::streamsize>(feature_dim_ * sizeof(float)))) {
            return truncated("centroids");
        }
    }

    inverted_lists_.resize(nlist_);
    for (uint32_t i = 0; i < nlist_; ++i) {
        uint64_t list_size = 0;
        uint64_t num_vectors = 0;
        if (!f.read(reinterpret_cast<char*>(&list_size), 8)
            || !f.read(reinterpret_cast<char*>(&num_vectors), 8)) {
            return truncated("inverted list header");
        }
        // 每个向量的数据量必须放得进 list_size 且总量有界，避免坏文件
        // 触发超大分配。
        const uint64_t bytes_per_vector = sizeof(uint32_t) + feature_dim_ * sizeof(float);
        if (num_vectors > 200000000ull
            || num_vectors * bytes_per_vector > list_size + bytes_per_vector) {
            spdlog::error("FAISS IVF index list {} has invalid size {} / {} vectors",
                          i, list_size, num_vectors);
            return false;
        }

        inverted_lists_[i].resize(num_vectors);
        for (uint64_t j = 0; j < num_vectors; ++j) {
            inverted_lists_[i][j].resize(feature_dim_);
            if (!f.read(reinterpret_cast<char*>(inverted_lists_[i][j].data()),
                        static_cast<std::streamsize>(feature_dim_ * sizeof(float)))) {
                return truncated("inverted list vectors");
            }
        }
    }

    spdlog::info("Parsed {} centroid(s) and {} inverted list(s)", nlist_, nlist_);
    return true;
}

uint32_t IndexSearch::find_nearest_centroid(const std::vector<float>& vec) const {
    uint32_t best = 0;
    float best_dist = std::numeric_limits<float>::max();
    for (uint32_t i = 0; i < nlist_; ++i) {
        float dist = 0.0f;
        for (uint32_t j = 0; j < feature_dim_; ++j) {
            float d = vec[j] - centroids_[i][j];
            dist += d * d;
        }
        if (dist < best_dist) {
            best_dist = dist;
            best = i;
        }
    }
    return best;
}

std::vector<float> IndexSearch::search(
    const std::vector<float>& features,
    uint32_t feat_dim, float index_rate
) {
    if (!loaded() || index_rate <= 0.0f) {
        return features;
    }

    if (feat_dim != feature_dim_) {
        spdlog::warn("Feature dim {} != index dim {}, skip index", feat_dim, feature_dim_);
        return features;
    }

    size_t n_frames = features.size() / feat_dim;
    std::vector<float> result = features;

    for (size_t t = 0; t < n_frames; ++t) {
        std::vector<float> feat_vec(features.begin() + t * feat_dim,
                                     features.begin() + (t + 1) * feat_dim);
        uint32_t cid = find_nearest_centroid(feat_vec);

        if (inverted_lists_[cid].empty()) continue;

        uint32_t best_idx = 0;
        float best_dist = std::numeric_limits<float>::max();
        for (uint32_t k = 0; k < inverted_lists_[cid].size(); ++k) {
            float dist = 0.0f;
            for (uint32_t j = 0; j < feature_dim_; ++j) {
                float d = feat_vec[j] - inverted_lists_[cid][k][j];
                dist += d * d;
            }
            if (dist < best_dist) {
                best_dist = dist;
                best_idx = k;
            }
        }

        const auto& nearest = inverted_lists_[cid][best_idx];
        for (uint32_t j = 0; j < feature_dim_; ++j) {
            result[t * feat_dim + j] = feat_vec[j] * (1.0f - index_rate) +
                                        nearest[j] * index_rate;
        }
    }

    return result;
}

} // namespace rvc
