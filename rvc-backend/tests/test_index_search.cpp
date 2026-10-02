#include "rvc/index_search.hpp"
#include <spdlog/spdlog.h>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

// IndexSearch 单测：
//  1) 用与 faiss write_index 逐字节一致的写出器构造合成索引（"full"/"sprs"），
//     验证解析 + 检索结果与独立参考实现一致；
//  2) 加载由真实 faiss（rvc-backend/tests/gen_index_fixture.py）生成的 fixture，
//     对照脚本用 faiss 自身 search() 算出的期望混合结果；
//  3) 各类损坏/不支持的文件必须 load 失败而不是静默接受。

namespace fs = std::filesystem;
using rvc::IndexSearch;

static int g_failures = 0;

#define CHECK(cond) do { \
    if (!(cond)) { \
        std::cerr << "FAIL: " << #cond << " at " << __FILE__ << ":" << __LINE__ << "\n"; \
        ++g_failures; \
    } \
} while(0)

// ── 真实 faiss 布局的合成索引写出器 ──────────────────────────────────────────

struct SyntheticIndex {
    uint32_t d = 8;
    uint32_t nlist = 2;
    std::vector<float> centroids;           // nlist × d
    std::vector<std::vector<float>> lists;  // nlist 个表，每表若干 d 维向量
    std::string storage = "full";           // "full" | "sprs"
    // 可注入的破坏项（默认全部合法）
    std::string magic = "IwFl";
    uint64_t marker = 0x100000;
    uint8_t trained = 1;
    int32_t metric = 1;                     // 1 = METRIC_L2
    std::string quantizer_fourcc = "IxF2";
    uint8_t map_type = 0;
    std::string lists_fourcc = "ilar";
    std::string storage_fourcc = "full";
    size_t truncate_at = 0;                 // >0 时写到该字节数即截断
    size_t trailing = 0;                    // 末尾追加的垃圾字节数
};

static void put_u32(std::vector<uint8_t>& b, uint32_t v) {
    for (int i = 0; i < 4; ++i) b.push_back(static_cast<uint8_t>((v >> (8 * i)) & 0xff));
}
static void put_i64(std::vector<uint8_t>& b, int64_t v) {
    for (int i = 0; i < 8; ++i) b.push_back(static_cast<uint8_t>((static_cast<uint64_t>(v) >> (8 * i)) & 0xff));
}
static void put_f32(std::vector<uint8_t>& b, float v) {
    uint32_t bits;
    std::memcpy(&bits, &v, 4);
    put_u32(b, bits);
}
static void put_str(std::vector<uint8_t>& b, const std::string& s) {
    b.insert(b.end(), s.begin(), s.end());
}

static std::vector<uint8_t> build_index_bytes(const SyntheticIndex& spec) {
    std::vector<uint8_t> b;
    const uint32_t d = spec.d;
    uint64_t ntotal = 0;
    for (const auto& l : spec.lists) ntotal += l.size() / d;

    put_str(b, spec.magic);
    put_u32(b, d);
    put_i64(b, static_cast<int64_t>(ntotal));
    put_i64(b, static_cast<int64_t>(spec.marker));
    put_i64(b, static_cast<int64_t>(spec.marker));
    b.push_back(spec.trained);
    put_u32(b, static_cast<uint32_t>(spec.metric));
    put_i64(b, spec.nlist);
    put_i64(b, 1);  // nprobe

    put_str(b, spec.quantizer_fourcc);
    put_u32(b, d);
    put_i64(b, spec.nlist);
    put_i64(b, static_cast<int64_t>(spec.marker));
    put_i64(b, static_cast<int64_t>(spec.marker));
    b.push_back(1);
    put_u32(b, static_cast<uint32_t>(spec.metric));
    put_i64(b, static_cast<int64_t>(spec.nlist * d));  // 质心 float 个数
    for (float c : spec.centroids) put_f32(b, c);

    b.push_back(spec.map_type);
    put_i64(b, 0);  // 空 direct map array

    put_str(b, spec.lists_fourcc);
    put_i64(b, spec.nlist);
    put_i64(b, static_cast<int64_t>(d) * 4);
    if (spec.storage == "full") {
        put_str(b, spec.storage_fourcc);
        put_i64(b, spec.nlist);
        for (const auto& l : spec.lists) put_i64(b, static_cast<int64_t>(l.size() / d));
    } else {  // "sprs"：只写非空表
        put_str(b, spec.storage_fourcc);
        std::vector<std::pair<uint64_t, uint64_t>> pairs;
        for (uint32_t i = 0; i < spec.nlist; ++i) {
            if (!spec.lists[i].empty()) {
                pairs.emplace_back(i, spec.lists[i].size() / d);
            }
        }
        put_i64(b, static_cast<int64_t>(pairs.size()));
        for (const auto& [idx, n] : pairs) {
            put_i64(b, static_cast<int64_t>(idx));
            put_i64(b, static_cast<int64_t>(n));
        }
    }
    int64_t id = 0;
    for (const auto& l : spec.lists) {
        for (float v : l) put_f32(b, v);
        for (size_t k = 0; k < l.size() / d; ++k) put_i64(b, id++);
    }
    if (spec.trailing) {
        for (size_t i = 0; i < spec.trailing; ++i) b.push_back(0x5a);
    }
    if (spec.truncate_at > 0 && spec.truncate_at < b.size()) {
        b.resize(spec.truncate_at);
    }
    return b;
}

static bool write_file(const fs::path& p, const std::vector<uint8_t>& bytes) {
    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f.write(reinterpret_cast<const char*>(bytes.data()),
            static_cast<std::streamsize>(bytes.size()));
    return static_cast<bool>(f);
}

// 独立参考实现：最近质心 → 表内最近向量 → 线性混合。
static std::vector<float> reference_search(const SyntheticIndex& spec,
                                           const std::vector<float>& feats,
                                           float rate) {
    const uint32_t d = spec.d;
    std::vector<float> out = feats;
    const size_t frames = feats.size() / d;
    for (size_t t = 0; t < frames; ++t) {
        const float* q = feats.data() + t * d;
        uint32_t best_c = 0;
        float best_cd = std::numeric_limits<float>::max();
        for (uint32_t c = 0; c < spec.nlist; ++c) {
            float dist = 0;
            for (uint32_t j = 0; j < d; ++j) {
                const float e = q[j] - spec.centroids[c * d + j];
                dist += e * e;
            }
            if (dist < best_cd) { best_cd = dist; best_c = c; }
        }
        if (spec.lists[best_c].empty()) continue;
        uint64_t best_k = 0;
        float best_d = std::numeric_limits<float>::max();
        const uint64_t n = spec.lists[best_c].size() / d;
        for (uint64_t k = 0; k < n; ++k) {
            float dist = 0;
            for (uint32_t j = 0; j < d; ++j) {
                const float e = q[j] - spec.lists[best_c][k * d + j];
                dist += e * e;
            }
            if (dist < best_d) { best_d = dist; best_k = k; }
        }
        for (uint32_t j = 0; j < d; ++j) {
            out[t * d + j] = q[j] * (1.0f - rate) + spec.lists[best_c][best_k * d + j] * rate;
        }
    }
    return out;
}

static bool close_enough(const std::vector<float>& a, const std::vector<float>& bv, float eps) {
    if (a.size() != bv.size()) return false;
    for (size_t i = 0; i < a.size(); ++i) {
        if (std::fabs(a[i] - bv[i]) > eps) return false;
    }
    return true;
}

// ── fixture 文本格式解析（由 gen_index_fixture.py 生成）─────────────────────

struct FixtureData {
    uint32_t d = 0;
    uint32_t nlist = 0;
    float rate = 0.75f;
    std::vector<std::vector<float>> queries;
    std::vector<std::vector<float>> expected;
};

static bool load_fixture(const fs::path& p, FixtureData& out) {
    std::ifstream f(p);
    if (!f) return false;
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream is(line);
        std::string tag;
        is >> tag;
        if (tag == "d") is >> out.d;
        else if (tag == "nlist") is >> out.nlist;
        else if (tag == "rate") is >> out.rate;
        else if (tag == "query" || tag == "expected") {
            std::vector<float> vec;
            float v;
            while (is >> v) vec.push_back(v);
            if (tag == "query") out.queries.push_back(std::move(vec));
            else out.expected.push_back(std::move(vec));
        }
    }
    return out.d > 0 && !out.queries.empty() && out.queries.size() == out.expected.size();
}

// ── 测试 ─────────────────────────────────────────────────────────────────────

static SyntheticIndex make_two_cluster_spec() {
    SyntheticIndex spec;
    spec.d = 8;
    spec.nlist = 2;
    spec.centroids = {
        -10, -10, -10, -10, -10, -10, -10, -10,
         10,  10,  10,  10,  10,  10,  10,  10,
    };
    spec.lists = {
        {-9.5f, -10.5f, -9.5f, -10.5f, -9.5f, -10.5f, -9.5f, -10.5f,
         -10.5f, -9.5f, -10.5f, -9.5f, -10.5f, -9.5f, -10.5f, -9.5f},
        {9.5f, 10.5f, 9.5f, 10.5f, 9.5f, 10.5f, 9.5f, 10.5f,
         10.5f, 9.5f, 10.5f, 9.5f, 10.5f, 9.5f, 10.5f, 9.5f},
    };
    return spec;
}

static void test_synthetic_full(const fs::path& dir) {
    std::cout << "[test] synthetic 'full' index parse + search...\n";
    const auto spec = make_two_cluster_spec();
    const auto p = dir / "synthetic_full.index";
    CHECK(write_file(p, build_index_bytes(spec)));

    IndexSearch idx;
    CHECK(idx.load(p, spec.d));
    CHECK(idx.loaded());

    const std::vector<float> feats = {
        // 两帧贴近簇 0，两帧贴近簇 1
        -9.8f, -10.2f, -9.8f, -10.2f, -9.8f, -10.2f, -9.8f, -10.2f,
        -10.3f, -9.9f, -10.3f, -9.9f, -10.3f, -9.9f, -10.3f, -9.9f,
         9.9f, 10.1f, 9.9f, 10.1f, 9.9f, 10.1f, 9.9f, 10.1f,
        10.2f, 9.8f, 10.2f, 9.8f, 10.2f, 9.8f, 10.2f, 9.8f,
    };
    const auto got = idx.search(feats, spec.d, 0.75f);
    const auto want = reference_search(spec, feats, 0.75f);
    CHECK(close_enough(got, want, 1e-6f));
    std::cout << "  [OK]\n";
}

static void test_synthetic_sprs(const fs::path& dir) {
    std::cout << "[test] synthetic 'sprs' index (empty list passthrough)...\n";
    auto spec = make_two_cluster_spec();
    spec.storage = "sprs";
    spec.storage_fourcc = "sprs";
    spec.lists[0].clear();  // 表 0 为空：命中它的查询必须原样透传
    const auto p = dir / "synthetic_sprs.index";
    CHECK(write_file(p, build_index_bytes(spec)));

    IndexSearch idx;
    CHECK(idx.load(p, spec.d));

    const std::vector<float> feats = {
        -9.8f, -10.2f, -9.8f, -10.2f, -9.8f, -10.2f, -9.8f, -10.2f,  // 命中空表
         9.9f, 10.1f, 9.9f, 10.1f, 9.9f, 10.1f, 9.9f, 10.1f,         // 命中表 1
    };
    const auto got = idx.search(feats, spec.d, 0.6f);
    const auto want = reference_search(spec, feats, 0.6f);
    CHECK(close_enough(got, want, 1e-6f));
    std::cout << "  [OK]\n";
}

static void test_corrupt_files(const fs::path& dir) {
    std::cout << "[test] corrupt / unsupported files must fail to load...\n";
    struct Case {
        const char* name;
        SyntheticIndex spec;
    };
    std::vector<Case> cases;
    {
        SyntheticIndex s; s.magic = "IxFl";          cases.push_back({"bad_magic", s});
        SyntheticIndex s2; s2.marker = 0;            cases.push_back({"bad_marker", s2});
        SyntheticIndex s3; s3.trained = 0;           cases.push_back({"untrained", s3});
        SyntheticIndex s4; s4.metric = 0;            cases.push_back({"metric_ip", s4});
        SyntheticIndex s5; s5.quantizer_fourcc = "IxPQ"; cases.push_back({"bad_quantizer", s5});
        SyntheticIndex s6; s6.map_type = 1;          cases.push_back({"direct_map", s6});
        SyntheticIndex s7; s7.lists_fourcc = "il00"; cases.push_back({"no_ilar", s7});
        SyntheticIndex s8; s8.storage_fourcc = "ilsq"; cases.push_back({"bad_storage", s8});
        SyntheticIndex s9; s9.trailing = 7;          cases.push_back({"trailing", s9});
    }
    for (const auto& c : cases) {
        const auto p = dir / (std::string("corrupt_") + c.name + ".index");
        if (!write_file(p, build_index_bytes(c.spec))) {
            CHECK(false);
            continue;
        }
        IndexSearch idx;
        if (idx.load(p)) {
            std::cerr << "  case '" << c.name << "' unexpectedly loaded\n";
            CHECK(false);
        }
    }
    // 截断：先写完整文件再裁掉末尾若干字节
    {
        const auto full = build_index_bytes(make_two_cluster_spec());
        for (const size_t cut : {size_t{1}, size_t{40},
                                 full.size() / size_t{2}, full.size() - size_t{1}}) {
            SyntheticIndex s;
            s.truncate_at = full.size() - cut;
            const auto p = dir / "corrupt_truncated.index";
            write_file(p, build_index_bytes(s));
            IndexSearch idx;
            if (idx.load(p)) {
                std::cerr << "  truncated (cut " << cut << ") unexpectedly loaded\n";
                CHECK(false);
            }
        }
    }
    // 不存在的文件：返回 false 而不是抛异常
    {
        IndexSearch idx;
        CHECK(!idx.load(dir / "definitely_missing.index"));
    }
    std::cout << "  [OK]\n";
}

static void test_search_semantics(const fs::path& dir) {
    std::cout << "[test] search passthrough semantics...\n";
    const auto spec = make_two_cluster_spec();
    const auto p = dir / "semantics.index";
    CHECK(write_file(p, build_index_bytes(spec)));
    IndexSearch idx;
    CHECK(idx.load(p, spec.d));

    const std::vector<float> feats(spec.d * 2, 0.25f);
    // index_rate=0 → 原样返回
    CHECK(idx.search(feats, spec.d, 0.0f) == feats);
    // 维度不匹配 → 原样返回
    CHECK(idx.search(feats, spec.d + 1, 0.5f) == feats);
    // 未加载 → 原样返回
    IndexSearch empty;
    CHECK(empty.search(feats, spec.d, 0.5f) == feats);
    std::cout << "  [OK]\n";
}

static void test_allocation_budget_and_sparse_ids(const fs::path& dir) {
    std::cout << "[test] centroid allocation budget and duplicate sparse ids...\n";
    auto oversized = make_two_cluster_spec();
    oversized.nlist = 1000000;
    const auto large_path = dir / "oversized_centroids.index";
    CHECK(write_file(large_path, build_index_bytes(oversized)));
    IndexSearch idx;
    CHECK(!idx.load(large_path));
    CHECK(!idx.loaded());

    auto sparse = make_two_cluster_spec();
    sparse.storage = "sprs";
    sparse.storage_fourcc = "sprs";
    auto bytes = build_index_bytes(sparse);
    const std::string marker = "ilar";
    const auto it = std::search(bytes.begin(), bytes.end(), marker.begin(), marker.end());
    CHECK(it != bytes.end());
    const auto second_id = static_cast<size_t>(it - bytes.begin()) + 48;
    CHECK(second_id + 8 <= bytes.size());
    std::fill(bytes.begin() + second_id, bytes.begin() + second_id + 8, 0);
    const auto duplicate_path = dir / "duplicate_sparse.index";
    CHECK(write_file(duplicate_path, bytes));
    CHECK(!idx.load(duplicate_path));
    CHECK(!idx.loaded());
}

static void test_real_faiss_fixture() {
    std::cout << "[test] real faiss-generated fixture (d=768)...\n";
    const fs::path base = fs::path(__FILE__).parent_path() / "fixtures";
    const auto index_path = base / "rvc_ivf_flat_d768.index";
    const auto data_path = base / "rvc_ivf_flat_d768.txt";
    if (!fs::exists(index_path) || !fs::exists(data_path)) {
        std::cerr << "  fixture missing; run tests/gen_index_fixture.py\n";
        CHECK(false);
        return;
    }
    FixtureData data;
    CHECK(load_fixture(data_path, data));
    CHECK(data.d == 768);

    IndexSearch idx;
    CHECK(idx.load(index_path, data.d));

    for (size_t i = 0; i < data.queries.size(); ++i) {
        const auto got = idx.search(data.queries[i], data.d, data.rate);
        CHECK(close_enough(got, data.expected[i], 1e-4f));
    }
    std::cout << "  [OK] " << data.queries.size() << " queries matched faiss ground truth\n";
}

int main() {
    spdlog::set_level(spdlog::level::off);
    const auto dir = fs::temp_directory_path() / "mozart_test_index";
    fs::create_directories(dir);

    test_synthetic_full(dir);
    test_synthetic_sprs(dir);
    test_corrupt_files(dir);
    test_search_semantics(dir);
    test_allocation_budget_and_sparse_ids(dir);
    test_real_faiss_fixture();

    fs::remove_all(dir);
    if (g_failures == 0) {
        std::cout << "\n[PASS] All index_search tests passed\n";
        return 0;
    }
    std::cout << "\n[FAIL] " << g_failures << " check(s) failed\n";
    return 1;
}
