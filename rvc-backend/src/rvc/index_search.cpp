#include "rvc/index_search.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <cmath>
#include <cstring>
#include <algorithm>
#include <limits>
#include <stdexcept>

namespace rvc {

// ─────────────────────────────────────────────────────────────────────────────
// FAISS IndexIVFFlat（"IwFl"）序列化布局。
//
// 依据：faiss v1.7.2/v1.7.4/main 的 faiss/impl/index_write.cpp、index_read.cpp，
// 以及本地 faiss 1.15.1 生成文件 + HuggingFace 真实 RVC .index（added_IVF*_Flat_
// nprobe_1_*）的逐字节核对。四个版本布局一致：
//
//   u32  fourcc "IwFl"
//   i32  d
//   i64  ntotal
//   i64  dummy = 0x100000        （faiss 预留标记，恒为 1<<20，两处）
//   i64  dummy = 0x100000
//   u8   is_trained
//   i32  metric_type（0=IP，1=L2；>1 时还写 metric_arg，本实现拒绝）
//   i64  nlist
//   i64  nprobe
//   ── 内嵌量化器（IndexFlatL2 / IndexFlatIP）──
//   u32  fourcc "IxF2"（L2）或 "IxFI"（IP）
//   i32  d                      （== d）
//   i64  ntotal                 （== nlist）
//   i64  dummy ×2 = 0x100000
//   u8   is_trained
//   i32  metric_type
//   i64  质心元素数              （旧版写 float 个数 ntotal*d，新版部分写
//                                 字节数 ntotal*d*4，两者都接受）
//   f32  质心，nlist*d 个，行主序
//   ── direct map（RVC 索引恒为空）──
//   u8   map type（0=NoMap；1/2 时后面还跟变长数据，本实现拒绝）
//   i64  array 长度（0）
//   ── 倒排表 ──
//   u32  fourcc "ilar"
//   i64  nlist                  （== nlist）
//   i64  code_size              （== d*4）
//   u32  "full"（稠密）| "sprs"（稀疏）
//   full: i64 count（== nlist）+ count × i64 表大小
//   sprs: i64 对数 + 每对 (i64 表号, i64 大小)，未列出的表为空
//   每表：n*code_size 字节向量数据（float 原码），随后 n × i64 向量 id
// ─────────────────────────────────────────────────────────────────────────────

namespace {

constexpr uint32_t kHeaderDummy = 0x100000;   // faiss 预留标记 1<<20
constexpr uint64_t kMaxTotalVectors = 100000000ull;  // 1e8 × 3KB ≈ 300GB，纯防御

// 带边界检查的小端流式读取器；越界/超限一律抛异常，由 load() 统一记录。
class StreamReader {
public:
    explicit StreamReader(std::ifstream& f, uint64_t file_size)
        : f_(f), file_size_(file_size) {}

    void read(void* dst, size_t n) {
        if (pos_ + n > file_size_) {
            throw std::runtime_error("file truncated (need " + std::to_string(n)
                                     + " bytes at offset " + std::to_string(pos_)
                                     + ", file is " + std::to_string(file_size_) + ")");
        }
        f_.read(reinterpret_cast<char*>(dst), static_cast<std::streamsize>(n));
        if (!f_) throw std::runtime_error("read failed at offset " + std::to_string(pos_));
        pos_ += n;
    }

    void skip(size_t n) {
        char scratch[256];
        while (n > 0) {
            const size_t chunk = std::min(n, sizeof(scratch));
            read(scratch, chunk);
            n -= chunk;
        }
    }

    uint8_t u8() { uint8_t v; read(&v, 1); return v; }
    int32_t i32() { int32_t v; read(&v, 4); return v; }
    int64_t i64() { int64_t v; read(&v, 8); return v; }

    std::string fourcc() {
        char c[4];
        read(c, 4);
        return std::string(c, 4);
    }

    void expect_fourcc(const char* expected, const char* what) {
        const auto got = fourcc();
        if (got != expected) {
            throw std::runtime_error(std::string(what) + ": expected fourcc '"
                                     + expected + "', got '" + got + "'");
        }
    }

    float f32() { float v; read(&v, 4); return v; }

    uint64_t pos() const { return pos_; }
    uint64_t remaining() const { return file_size_ - pos_; }

private:
    std::ifstream& f_;
    uint64_t file_size_;
    uint64_t pos_ = 0;
};

} // namespace

bool IndexSearch::load(const std::filesystem::path& index_path, uint32_t feature_dim) {
    nlist_ = 0;
    ntotal_ = 0;
    centroids_.clear();
    codes_.clear();
    list_offsets_.clear();
    feature_dim_ = feature_dim;

    if (!std::filesystem::exists(index_path)) {
        spdlog::warn("Index file not found: {}", index_path.string());
        return false;
    }

    try {
        if (!parse_ivf_index(index_path)) {
            return false;
        }
        spdlog::info("Index loaded: {} (nlist={}, ntotal={}, dim={})",
            index_path.filename().string(), nlist_, ntotal_, feature_dim_);
        return true;
    } catch (const std::exception& e) {
        nlist_ = 0;
        ntotal_ = 0;
        centroids_.clear();
        codes_.clear();
        list_offsets_.clear();
        spdlog::error("Failed to load index {}: {}", index_path.string(), e.what());
        return false;
    }
}

bool IndexSearch::parse_ivf_index(const std::filesystem::path& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    const auto file_size = static_cast<uint64_t>(std::filesystem::file_size(path));

    StreamReader r(f, file_size);

    r.expect_fourcc("IwFl", "not an FAISS IndexIVFFlat index");

    const int32_t d = r.i32();
    if (d <= 0 || d > 65536) {
        spdlog::error("FAISS IVF index has invalid header: d={}", d);
        return false;
    }
    feature_dim_ = static_cast<uint32_t>(d);

    const int64_t ntotal = r.i64();
    if (ntotal < 0 || static_cast<uint64_t>(ntotal) > kMaxTotalVectors) {
        throw std::runtime_error("invalid ntotal " + std::to_string(ntotal));
    }
    ntotal_ = static_cast<uint64_t>(ntotal);

    if (r.i64() != kHeaderDummy || r.i64() != kHeaderDummy) {
        throw std::runtime_error(
            "missing FAISS header marker (0x100000); only faiss >= 1.7.0 "
            "IndexIVFFlat files are supported");
    }

    if (r.u8() == 0) {
        throw std::runtime_error("index is not trained");
    }
    const int32_t metric = r.i32();
    if (metric != 1) {  // faiss METRIC_L2
        throw std::runtime_error("unsupported metric_type " + std::to_string(metric)
                                 + " (only L2, as used by RVC, is supported)");
    }

    const int64_t nlist = r.i64();
    if (nlist <= 0 || nlist > 1000000) {
        throw std::runtime_error("invalid nlist " + std::to_string(nlist)
                                 + " for ntotal " + std::to_string(ntotal));
    }
    nlist_ = static_cast<uint32_t>(nlist);
    r.i64();  // nprobe：检索端恒按 nprobe=1（RVC 约定）逐表精确搜索

    // ── 内嵌量化器 ──
    const auto qfourcc = r.fourcc();
    if (qfourcc != "IxF2" && qfourcc != "IxFI") {
        throw std::runtime_error("unsupported quantizer fourcc '" + qfourcc
                                 + "' (expected flat L2/IP)");
    }
    const int32_t qd = r.i32();
    const int64_t qntotal = r.i64();
    if (qd != d || qntotal != nlist) {
        throw std::runtime_error("quantizer header mismatch: d=" + std::to_string(qd)
                                 + " ntotal=" + std::to_string(qntotal));
    }
    if (r.i64() != kHeaderDummy || r.i64() != kHeaderDummy) {
        throw std::runtime_error("missing quantizer header marker");
    }
    if (r.u8() == 0) {
        throw std::runtime_error("quantizer is not trained");
    }
    const int32_t qmetric = r.i32();
    if (qmetric != metric) {
        throw std::runtime_error("quantizer metric " + std::to_string(qmetric)
                                 + " disagrees with index metric");
    }

    const int64_t count = r.i64();
    const int64_t floats_expected = nlist * d;
    if (count != floats_expected && count != floats_expected * 4) {
        throw std::runtime_error("unexpected quantizer codes count " + std::to_string(count));
    }

    centroids_.resize(static_cast<size_t>(floats_expected));
    for (auto& c : centroids_) c = r.f32();

    // ── direct map ──
    const uint8_t map_type = r.u8();
    if (map_type != 0) {
        throw std::runtime_error("unsupported direct map type " + std::to_string(map_type));
    }
    if (r.i64() != 0) {
        throw std::runtime_error("direct map array must be empty");
    }

    // ── 倒排表 ──
    r.expect_fourcc("ilar", "unsupported inverted lists storage");

    const int64_t il_nlist = r.i64();
    const int64_t code_size = r.i64();
    if (il_nlist != nlist || code_size != static_cast<int64_t>(d) * 4) {
        throw std::runtime_error("inverted lists header mismatch: nlist="
                                 + std::to_string(il_nlist) + " code_size="
                                 + std::to_string(code_size));
    }

    std::vector<uint64_t> sizes(static_cast<size_t>(nlist), 0);
    const auto list_type = r.fourcc();
    if (list_type == "full") {
        if (r.i64() != nlist) {
            throw std::runtime_error("'full' list count mismatch");
        }
        for (auto& s : sizes) {
            const int64_t v = r.i64();
            if (v < 0) throw std::runtime_error("negative list size");
            s = static_cast<uint64_t>(v);
        }
    } else if (list_type == "sprs") {
        const int64_t pairs = r.i64();
        if (pairs < 0 || static_cast<uint64_t>(pairs) > kMaxTotalVectors) {
            throw std::runtime_error("invalid 'sprs' pair count");
        }
        for (int64_t i = 0; i < pairs; ++i) {
            const int64_t idx = r.i64();
            const int64_t v = r.i64();
            if (idx < 0 || idx >= nlist || v < 0) {
                throw std::runtime_error("invalid 'sprs' entry");
            }
            sizes[static_cast<size_t>(idx)] = static_cast<uint64_t>(v);
        }
    } else {
        throw std::runtime_error("unsupported list storage '" + list_type
                                 + "' (expected 'full' or 'sprs')");
    }

    uint64_t total = 0;
    for (const auto s : sizes) {
        total += s;
        if (total > kMaxTotalVectors) {
            throw std::runtime_error("list sizes exceed sanity bound");
        }
    }
    if (total != ntotal_) {
        throw std::runtime_error("list sizes sum " + std::to_string(total)
                                 + " != ntotal " + std::to_string(ntotal));
    }

    // 每表：n × code_size 字节向量数据 + n × 8 字节 id。
    // 先按剩余字节数校验总预算，再逐表读取，避免坏文件触发超大分配。
    const uint64_t bytes_per_vector = static_cast<uint64_t>(d) * 4 + 8;
    if (total > r.remaining() / bytes_per_vector) {
        throw std::runtime_error("file too small for " + std::to_string(total)
                                 + " vectors (truncated?)");
    }

    codes_.resize(static_cast<size_t>(ntotal_) * static_cast<size_t>(d));
    list_offsets_.assign(static_cast<size_t>(nlist) + 1, 0);
    uint64_t written = 0;
    std::vector<int64_t> id_scratch;
    for (uint32_t i = 0; i < nlist_; ++i) {
        list_offsets_[i] = written;
        const uint64_t n = sizes[i];
        if (n == 0) continue;
        float* dst = codes_.data() + written * static_cast<uint64_t>(d);
        r.read(dst, static_cast<size_t>(n) * static_cast<size_t>(d) * 4);
        id_scratch.resize(static_cast<size_t>(n));
        r.read(id_scratch.data(), static_cast<size_t>(n) * 8);
        written += n;
    }
    list_offsets_[nlist_] = written;
    if (r.remaining() != 0) {
        throw std::runtime_error(std::to_string(r.remaining())
                                 + " unexpected trailing bytes");
    }

    spdlog::info("Parsed {} centroid(s), {} vector(s)", nlist_, ntotal_);
    return true;
}

uint32_t IndexSearch::find_nearest_centroid(const float* vec) const {
    uint32_t best = 0;
    float best_dist = std::numeric_limits<float>::max();
    const size_t dim = feature_dim_;
    for (uint32_t i = 0; i < nlist_; ++i) {
        const float* c = centroids_.data() + static_cast<size_t>(i) * dim;
        float dist = 0.0f;
        for (size_t j = 0; j < dim; ++j) {
            const float dv = vec[j] - c[j];
            dist += dv * dv;
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
    const size_t dim = feature_dim_;

    for (size_t t = 0; t < n_frames; ++t) {
        const float* feat_vec = features.data() + t * feat_dim;
        const uint32_t cid = find_nearest_centroid(feat_vec);

        const uint64_t begin = list_offsets_[cid];
        const uint64_t end = list_offsets_[cid + 1];
        if (begin == end) continue;

        // nprobe=1 语义（RVC 索引文件名即 nprobe_1）：只在最近质心的表内精确搜索
        uint64_t best_idx = begin;
        float best_dist = std::numeric_limits<float>::max();
        for (uint64_t k = begin; k < end; ++k) {
            const float* cand = codes_.data() + k * dim;
            float dist = 0.0f;
            for (size_t j = 0; j < dim; ++j) {
                const float dv = feat_vec[j] - cand[j];
                dist += dv * dv;
            }
            if (dist < best_dist) {
                best_dist = dist;
                best_idx = k;
            }
        }

        const float* nearest = codes_.data() + best_idx * dim;
        for (size_t j = 0; j < dim; ++j) {
            result[t * feat_dim + j] = feat_vec[j] * (1.0f - index_rate) +
                                        nearest[j] * index_rate;
        }
    }

    return result;
}

} // namespace rvc
