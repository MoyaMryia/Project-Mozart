#include <mozart/source/mock_source.hpp>
#include <mozart/rvc/rvc_pipeline.hpp>
#include <mozart/output/dummy_sink.hpp>
#include <iostream>
#include <cmath>

using namespace mozart;

int main() {
    // Test: Mock source -> Mock pipeline -> Dummy sink
    MockSource source(440.0f);
    MockRVCPipeline pipeline;
    DummySink sink;

    FrameBuf in_frame;
    OutputFrameBuf out_frame;

    // Process 3 frames
    for (int i = 0; i < 3; ++i) {
        bool got_frame = source.poll(in_frame, 10000);
        assert(got_frame);
        assert(in_frame.meta.frame_idx == static_cast<uint32_t>(i));
        assert(in_frame.meta.vad_flag == 1);

        // Verify 440Hz sine wave
        for (size_t j = 0; j < in_frame.samples.size(); ++j) {
            float expected = std::sin(2.0 * M_PI * 440.0 * (i * 320 + j) / 16000.0);
            assert(std::abs(in_frame.samples[j] - expected) < 1e-5f);
        }

        // Process through mock pipeline (upsampling)
        pipeline.process(in_frame, out_frame);

        // Verify output is 960 samples (3x upsampling)
        assert(out_frame.samples.size() == 960);

        // Write to dummy sink
        sink.write(out_frame);
    }

    std::cout << "[PASS] Mock pipeline end-to-end (3 frames)\n";
    std::cout << "All mock_pipeline tests passed!\n";
    return 0;
}
