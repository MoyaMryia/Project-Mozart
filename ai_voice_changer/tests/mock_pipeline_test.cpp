// tests/mock_pipeline_test.cpp
#include "contract/contract_source.hpp"
#include "contract/mock_source.hpp"
#include "frame_meta.hpp"
#include "output/dummy_sink.hpp"
#include "rvc/rvc_pipeline.hpp"

#include <cstdio>
#include <string>
#include <vector>

int main() {
    using namespace mozart;

    MockSource source(/*tone_hz=*/440.0f, /*sample_rate=*/16000);
    auto pipeline = make_pipeline(/*mock_mode=*/true,
                                   /*assets_dir=*/"", /*hubert_path=*/"",
                                   /*rmvpe_path=*/"", /*models_dir=*/"",
                                   /*device=*/"", /*index_rate=*/0.0f);
    DummySink sink;

    if (std::string(pipeline->name()) != "mock-pipeline") {
        std::fprintf(stderr, "FAIL: expected mock-pipeline, got %s\n",
                     pipeline->name());
        return 1;
    }

    ContractFrame in{};
    FrameMeta meta{};
    std::vector<float> out(kOutputSamples);

    auto rc = source.poll(in, meta, /*timeout_us=*/0);
    if (rc != PollResult::Ok) {
        std::fprintf(stderr, "FAIL: mock source poll rc=%d\n",
                     static_cast<int>(rc));
        return 1;
    }
    if (meta.vad_flag != 1) {
        std::fprintf(stderr, "FAIL: tone mock should be voiced\n");
        return 1;
    }
    if (kContractSamples != 320) {
        std::fprintf(stderr, "FAIL: kContractSamples=%zu != 320\n",
                     kContractSamples);
        return 1;
    }

    pipeline->run(std::span<const float>(in), meta, std::span<float>(out));
    if (out.size() != static_cast<size_t>(kOutputSamples)) {
        std::fprintf(stderr, "FAIL: out size mismatch %zu != %d\n",
                     out.size(), kOutputSamples);
        return 1;
    }

    // verify no all-zero output for voiced tone input
    bool any_nonzero = false;
    for (float v : out) {
        if (v != 0.0f) { any_nonzero = true; break; }
    }
    if (!any_nonzero) {
        std::fprintf(stderr, "FAIL: mock pipeline produced all zero\n");
        return 1;
    }

    sink.write(out);
    if (sink.frames_written() != 1) {
        std::fprintf(stderr, "FAIL: frames_written=%llu\n",
                     static_cast<unsigned long long>(sink.frames_written()));
        return 1;
    }

    std::printf("mock_pipeline_test PASS: %zu in -> %zu out, frames=%llu\n",
                kContractSamples, out.size(),
                static_cast<unsigned long long>(sink.frames_written()));
    return 0;
}