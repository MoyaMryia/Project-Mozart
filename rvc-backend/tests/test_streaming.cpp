// test_streaming.cpp — StreamingRvc 无框架单测
// ============================================================================
// 用确定性假管线（输入最近邻 3x 上采样）验证：
//   1. 块调度节奏：攒满 window 出首块，此后每 hop 一块
//   2. 逐样本连续性：流式输出 == 整段直接 3x 上采样（交叉淡化区域两块
//      共享同一段输入、数值相等，混合后无失真）
//   3. 静音窗跳过：VAD 全零窗口跳推理、输出等长静音
//   4. frame_idx 缺口触发重置且流恢复
#include "rvc/streaming_pipeline.hpp"

#include <cmath>
#include <cstdio>
#include <vector>

using namespace rvc;

namespace {

int g_fail = 0;
#define CHECK(cond, msg) do { \
    if (cond) printf("  ok  %s\n", msg); \
    else { printf("  FAIL %s\n", msg); g_fail++; } } while (0)

// 假管线：输入最近邻 3x 上采样（确定性，便于逐样本对拍）
class Upsample3Pipeline : public RVCPipelineBase {
public:
    std::vector<float> process(const std::vector<float>& audio) override {
        std::vector<float> out;
        out.reserve(audio.size() * 3);
        for (float s : audio) {
            out.push_back(s);
            out.push_back(s);
            out.push_back(s);
        }
        return out;
    }
    bool is_mock() const override { return false; } // 强制流式语义
};

mozart_input_frame_t make_frame(uint32_t idx, uint8_t vad, float value,
                                uint8_t segment = 1, uint64_t pts_ns = 0)
{
    mozart_input_frame_t f{};
    f.meta.frame_idx = idx;
    f.meta.vad_flag = vad;
    f.meta.segment_id = segment;
    f.meta.pts_ns = pts_ns;
    for (int i = 0; i < MOZART_INPUT_SAMPLES; ++i) f.pcm[i] = value;
    return f;
}

std::vector<float> drain(StreamingRvc& s)
{
    std::vector<float> out;
    float buf[MOZART_OUTPUT_SAMPLES];
    for (;;) {
        const size_t got = s.pop_output(buf, MOZART_OUTPUT_SAMPLES);
        if (got == 0) break;
        out.insert(out.end(), buf, buf + got);
    }
    return out;
}

} // namespace

int main()
{
    // ---- 用例 1+2：块调度 + 逐样本连续性 ----
    printf("test_window_scheduling_and_continuity\n");
    {
        StreamingRvc::Config cfg;
        StreamingRvc s(cfg);
        Upsample3Pipeline pipe;

        const size_t kFrames = 500;
        std::vector<float> pushed;
        pushed.reserve(kFrames * MOZART_INPUT_SAMPLES);
        uint32_t idx = 1;
        for (size_t k = 0; k < kFrames; ++k) {
            const float v = std::sin(0.01f * static_cast<float>(k));
            mozart_input_frame_t f = make_frame(idx++, 1, v);
            s.push(f);
            pushed.insert(pushed.end(), f.pcm, f.pcm + MOZART_INPUT_SAMPLES);
            while (s.try_process_one(pipe)) {}
        }
        while (s.try_process_one(pipe)) {}

        const size_t blocks = s.stats().blocks.load();
        // 首块需 window=32000（100 帧）；此后每 hop=31200（97.5 帧）一块
        CHECK(blocks == 5, "block cadence: 5 blocks for 500 frames");

        const std::vector<float> out = drain(s);
        const size_t emit_out = (cfg.window_samples - cfg.crossfade_out / 3) * 3;
        CHECK(out.size() == blocks * emit_out, "output == blocks * emit_out");

        // 逐样本对拍：out[j] 必须等于 pushed[j/3]（3x 上采样）
        double max_err = 0.0;
        const size_t check_n = std::min(out.size(), pushed.size() * 3);
        for (size_t j = 0; j < check_n; ++j) {
            const float ref = pushed[j / 3];
            max_err = std::max(max_err, static_cast<double>(std::fabs(out[j] - ref)));
        }
        CHECK(max_err < 1e-6, "streamed output matches direct 3x upsample");
        CHECK(max_err >= 0.0, "placeholder");
    }

    // ---- 用例 3：静音窗跳过 ----
    printf("test_silence_skip\n");
    {
        StreamingRvc::Config cfg;
        StreamingRvc s(cfg);
        Upsample3Pipeline pipe;

        uint32_t idx = 1;
        for (size_t k = 0; k < 200; ++k) { // 200 帧全静音（vad=0）
            s.push(make_frame(idx++, 0, 0.0f));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(s.stats().blocks.load() == 0, "no inference on silence");
        CHECK(s.stats().skipped_blocks.load() == 2, "2 windows skipped");

        const std::vector<float> out = drain(s);
        bool all_zero = true;
        for (float v : out) all_zero = all_zero && v == 0.0f;
        CHECK(all_zero && out.size() == 2 * 93120, "silence in -> equal silence out");
    }

    // ---- 用例 4：frame_idx 缺口重置 ----
    printf("test_discontinuity_reset\n");
    {
        StreamingRvc::Config cfg;
        StreamingRvc s(cfg);
        Upsample3Pipeline pipe;

        uint32_t idx = 1;
        for (size_t k = 0; k < 150; ++k) { // 先建立流：150 帧 → 1 块
            s.push(make_frame(idx++, 1, 0.1f));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(s.stats().blocks.load() == 1, "first block before gap");
        drain(s);

        idx += 10; // 制造 10 帧缺口
        for (size_t k = 0; k < 150; ++k) {
            s.push(make_frame(idx++, 1, 0.1f));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(s.stats().resets.load() == 1, "gap triggers one reset");
        // 重置后重新攒满窗：再出 1 块
        CHECK(s.stats().blocks.load() == 2, "stream recovers after reset");
        const std::vector<float> out = drain(s);
        CHECK(out.size() == 93120, "post-reset output present");
    }

    if (g_fail) {
        printf("\n%d test(s) FAILED\n", g_fail);
        return 1;
    }
    printf("\nall tests passed\n");
    return 0;
}
