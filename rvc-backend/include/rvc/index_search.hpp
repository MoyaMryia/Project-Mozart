#pragma once

#include <cstdint>
#include <vector>
#include <string>
#include <filesystem>

namespace rvc {

// 加载并检索 RVC 训练管线产出的 FAISS IndexIVFFlat（"IwFl"）索引文件。
// 序列化布局按 faiss 1.7.2～1.15 源码 + 真实 RVC .index 文件逐字节核对实现，
// 详见 index_search.cpp 顶部的布局注释。
class IndexSearch {
public:
    bool load(const std::filesystem::path& index_path, uint32_t feature_dim = 768);
    bool loaded() const { return !centroids_.empty(); }

    std::vector<float> search(const std::vector<float>& features,
                              uint32_t feat_dim, float index_rate);

private:
    uint32_t feature_dim_ = 768;
    uint32_t nlist_ = 0;
    uint64_t ntotal_ = 0;
    // 扁平存储：质心 nlist_ × feature_dim_；全部倒排向量按表顺序拼接。
    std::vector<float> centroids_;
    std::vector<float> codes_;
    std::vector<uint64_t> list_offsets_;  // nlist_ + 1 个，指向 codes_ 的表起点

    bool parse_ivf_index(const std::filesystem::path& path);
    uint32_t find_nearest_centroid(const float* vec) const;
};

} // namespace rvc
