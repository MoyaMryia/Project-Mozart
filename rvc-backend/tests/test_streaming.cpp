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

class RealtimeTailPipeline : public Upsample3Pipeline {
public:
    std::vector<float> process_realtime(
        const std::vector<float>& audio, const RvcRealtimeRequest& request
    ) override {
        ++calls;
        last_input_samples = audio.size();
        last_request = request;
        return RVCPipelineBase::process_realtime(audio, request);
    }

    size_t calls = 0;
    size_t last_input_samples = 0;
    RvcRealtimeRequest last_request;
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
        // 首块需 window=32000（100 帧）；此后每 hop=31040（97 帧）一块
        CHECK(blocks == 5, "block cadence: 5 blocks for 500 frames");
        CHECK(s.stats().input_overruns.load() == 0,
              "healthy rolling history is not reported as overrun");

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

    // ---- 用例 5：Golden full-history + lookahead 调度和 target crop ----
    printf("test_full_history_context_crop\n");
    {
        StreamingRvc::Config cfg;
        cfg.skip_silence = false;
        cfg.full_history = true;
        cfg.right_context_samples = 32000;
        cfg.guard_samples = 80;
        StreamingRvc s(cfg);
        Upsample3Pipeline pipe;

        constexpr size_t kFrames = 500;
        std::vector<float> pushed;
        pushed.reserve(kFrames * MOZART_INPUT_SAMPLES);
        for (size_t frame = 0; frame < kFrames; ++frame) {
            const float value = std::sin(0.01f * static_cast<float>(frame));
            const auto input = make_frame(
                static_cast<uint32_t>(frame + 1), 1, value
            );
            s.push(input);
            pushed.insert(
                pushed.end(), input.pcm, input.pcm + MOZART_INPUT_SAMPLES
            );
            while (s.try_process_one(pipe)) {}
        }

        const size_t blocks = s.stats().blocks.load();
        CHECK(blocks == 4, "full-history waits for 2s lookahead and guard");
        CHECK(s.stats().input_overruns.load() == 0,
              "full-history retains the complete prefix");
        const auto out = drain(s);
        const size_t emit = (cfg.window_samples - cfg.crossfade_out / 3) * 3;
        CHECK(out.size() == blocks * emit,
              "full-history emits one target hop per prefix");
        double max_err = 0.0;
        for (size_t sample = 0; sample < out.size(); ++sample) {
            max_err = std::max(max_err, static_cast<double>(
                std::fabs(out[sample] - pushed[sample / 3])
            ));
        }
        CHECK(max_err < 1e-6, "full-history crops the target timeline exactly");
    }

    // ---- 用例 6：quality 模式先积攒两块余量再开始播放 ----
    printf("test_quality_startup_buffer\n");
    {
        StreamingRvc::Config cfg;
        cfg.skip_silence = false;
        cfg.full_history = true;
        cfg.right_context_samples = 32000;
        cfg.guard_samples = 80;
        cfg.startup_buffer_blocks = 3;
        StreamingRvc s(cfg);
        Upsample3Pipeline pipe;

        uint32_t idx = 1;
        for (size_t frame = 0; frame < 201; ++frame) {
            s.push(make_frame(idx++, 1, 0.25f));
            while (s.try_process_one(pipe)) {}
        }
        float output[MOZART_OUTPUT_SAMPLES]{};
        CHECK(s.stats().blocks.load() == 1, "first quality block is ready");
        CHECK(s.pop_output(output, MOZART_OUTPUT_SAMPLES) == 0,
              "first block remains buffered");

        for (size_t frame = 0; frame < 97; ++frame) {
            s.push(make_frame(idx++, 1, 0.25f));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(s.stats().blocks.load() == 2, "second quality block is ready");
        CHECK(s.pop_output(output, MOZART_OUTPUT_SAMPLES) == 0,
              "second block remains buffered");

        for (size_t frame = 0; frame < 97; ++frame) {
            s.push(make_frame(idx++, 1, 0.25f));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(s.stats().blocks.load() == 3, "third quality block is ready");
        CHECK(s.pop_output(output, MOZART_OUTPUT_SAMPLES) == MOZART_OUTPUT_SAMPLES,
              "playback starts with two reserve blocks");
    }

    // ---- 用例 7：upstream realtime 只等一个短块，过去上下文补零 ----
    printf("test_upstream_realtime_short_block\n");
    {
        StreamingRvc::Config cfg;
        cfg.window_samples = 3840;       // 240 ms，匹配 20 ms IO 帧
        cfg.crossfade_out = 2400;        // 50 ms @48k
        cfg.upstream_realtime = true;
        cfg.past_context_samples = 40000; // 2.5 s，仅过去上下文
        cfg.sola_search_out = 480;       // 10 ms @48k
        cfg.skip_silence = false;
        StreamingRvc s(cfg);
        RealtimeTailPipeline pipe;

        uint32_t idx = 1;
        for (size_t frame = 0; frame < 11; ++frame) {
            s.push(make_frame(idx++, 1, 0.1f));
            CHECK(!s.try_process_one(pipe), "realtime waits until the 240ms block");
        }
        s.push(make_frame(idx++, 1, 0.1f));
        CHECK(s.try_process_one(pipe), "realtime starts after one 240ms block");
        CHECK(pipe.calls == 1, "one realtime inference call");
        CHECK(pipe.last_input_samples == 44800,
              "realtime input is 2.5s past + block + overlap + search");
        CHECK(pipe.last_request.block_samples_16k == 3840,
              "pipeline receives the explicit input block size");
        CHECK(pipe.last_request.skip_head_frames == 250,
              "pipeline receives the explicit past-frame crop");
        CHECK(pipe.last_request.return_frames == 30,
              "pipeline receives the explicit return-frame count");
        CHECK(pipe.last_request.output_samples == 14400,
              "pipeline returns block + overlap + SOLA search");
        auto first = drain(s);
        CHECK(first.size() == 11520, "realtime emits exactly one 240ms block");

        for (size_t frame = 0; frame < 12; ++frame) {
            s.push(make_frame(idx++, 1, 0.2f));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(pipe.calls == 2, "realtime cadence is one call per 240ms");
        const auto second = drain(s);
        CHECK(second.size() == 11520, "second realtime block has fixed length");
        bool finite = true;
        for (float value : second) finite = finite && std::isfinite(value);
        CHECK(finite, "SOLA output is finite");
        CHECK(s.stats().input_overruns.load() == 0,
              "rolling realtime history is pruned without overrun");
    }

    if (g_fail) {
        printf("\n%d test(s) FAILED\n", g_fail);
        return 1;
    }
    printf("\nall tests passed\n");
    return 0;
}
