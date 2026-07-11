// tests/onnx_smoke_test.cpp
// Verifies the OnnxEngine loads a session. Requires assets/{hubert_base.onnx}.
// Designed to fail gracefully with a clear message if assets are absent.
#include "inference/onnx_engine.hpp"
#include "utils/logging.hpp"

#include <cstdio>
#include <cstdlib>
#include <filesystem>

int main() {
    using namespace mozart;
    OnnxEngine engine("cuda");
    if (std::strcmp(engine.name(), "onnxruntime") != 0) {
        std::fprintf(stderr, "FAIL: engine name\n");
        return 1;
    }

    const char* hubert = std::getenv("MOZART_HUBERT_ONNX");
    std::string path = hubert ? hubert : "./assets/hubert_base.onnx";
    if (!std::filesystem::exists(path)) {
        std::fprintf(stderr,
                      "SKIP: %s missing (set MOZART_HUBERT_ONNX to test)\n",
                      path.c_str());
        return 77;  // CTest "SKIP" exit code
    }

    try {
        auto session = engine.create_session(path);
        std::printf("onnx_smoke_test PASS: session ready for %s\n",
                    path.c_str());
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "FAIL: create_session: %s\n", e.what());
        return 1;
    }
}