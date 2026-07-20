#include "rvc/feature_extractor.hpp"
#include <iostream>
#include <cassert>
#include <cmath>
#include <vector>

static int g_failures = 0;

#define CHECK(cond) do { \
    if (!(cond)) { \
        std::cerr << "FAIL: " << #cond << " at " << __FILE__ << ":" << __LINE__ << "\n"; \
        ++g_failures; \
    } \
} while(0)

#define CHECK_CLOSE(a, b, eps) do { \
    if (std::fabs((a) - (b)) > (eps)) { \
        std::cerr << "FAIL: |" << #a << " - " << #b << "| = " \
                  << std::fabs((a) - (b)) << " > " << (eps) \
                  << " at " << __FILE__ << ":" << __LINE__ << "\n"; \
        ++g_failures; \
    } \
} while(0)

void test_feature_extractor_init() {
    std::cout << "[test] FeatureExtractor init...\n";

    rvc::FeatureExtractor fe(
        "nonexistent_hubert.pt",
        std::nullopt,
        "cpu", false
    );
    CHECK(!fe.is_hubert_loaded());
    CHECK(!fe.is_rmvpe_loaded());

    std::cout << "  [OK]\n";
}

void test_extract_features_stub() {
    std::cout << "[test] extract_features stub fallback...\n";

    rvc::FeatureExtractor fe("nonexistent.pt", std::nullopt, "cpu", false);
    std::vector<float> audio(16000, 0.0f);
    auto feats = fe.extract_features(audio, 16000);

    size_t n_frames = audio.size() / 512;
    CHECK(feats.size() == n_frames * 768);

    float sum = 0.0f;
    for (auto f : feats) sum += f;
    CHECK_CLOSE(sum, 0.0f, 1e-6f);

    std::cout << "  [OK]\n";
}

void test_extract_f0_stub() {
    std::cout << "[test] extract_f0 stub fallback...\n";

    rvc::FeatureExtractor fe("nonexistent.pt", std::nullopt, "cpu", false);
    std::vector<float> audio(1024, 0.0f);
    auto f0 = fe.extract_f0(audio, 16000, "harvest");

    CHECK(f0.size() > 0);
    for (auto v : f0) {
        CHECK_CLOSE(v, 0.0f, 1e-6f);
    }

    std::cout << "  [OK]\n";
}

void test_extract_f0_method_fallback() {
    std::cout << "[test] extract_f0 unknown method fallback...\n";

    rvc::FeatureExtractor fe("nonexistent.pt", std::nullopt, "cpu", false);
    std::vector<float> audio(1024, 0.0f);
    auto f0 = fe.extract_f0(audio, 16000, "unknown_method");

    CHECK(f0.size() > 0);

    std::cout << "  [OK]\n";
}

void test_empty_audio() {
    std::cout << "[test] empty audio handling...\n";

    rvc::FeatureExtractor fe("nonexistent.pt", std::nullopt, "cpu", false);
    std::vector<float> empty;

    auto feats = fe.extract_features(empty, 16000);
    CHECK(feats.size() == 768);

    auto f0 = fe.extract_f0(empty, 16000, "rmvpe");
    CHECK(f0.size() == 1);

    std::cout << "  [OK]\n";
}

int main() {
    test_feature_extractor_init();
    test_extract_features_stub();
    test_extract_f0_stub();
    test_extract_f0_method_fallback();
    test_empty_audio();

    if (g_failures == 0) {
        std::cout << "\n[PASS] All feature_extractor tests passed\n";
        return 0;
    } else {
        std::cout << "\n[FAIL] " << g_failures << " test(s) failed\n";
        return 1;
    }
}
