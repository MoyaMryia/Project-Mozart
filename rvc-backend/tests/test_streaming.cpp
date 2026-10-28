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
#include <future>
#include <stdexcept>

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

    printf("test_realtime_playout_reserve_absorbs_compute_jitter\n");
    {
        StreamingRvc::Config base;
        base.window_samples = 3840;
        base.crossfade_out = 2400;
        base.upstream_realtime = true;
        base.skip_silence = true;
        auto buffered = base;
        buffered.startup_delay_out = 20 * MOZART_OUTPUT_SAMPLES;
        StreamingRvc immediate(base), reserved(buffered);
        RealtimeTailPipeline immediate_pipe, reserved_pipe;
        for (uint32_t i = 1; i <= 12; ++i) {
            immediate.push(make_frame(i, 0, 0.0f));
            reserved.push(make_frame(i, 0, 0.0f));
            immediate.pop_output(nullptr, MOZART_OUTPUT_SAMPLES);
            reserved.pop_output(nullptr, MOZART_OUTPUT_SAMPLES);
        }
        CHECK(immediate.try_process_one(immediate_pipe) && reserved.try_process_one(reserved_pipe),
              "first blocks are available before the synthetic compute delay");
        float output[MOZART_OUTPUT_SAMPLES]{};
        CHECK(reserved.pop_output(output, 0) == 0, "zero-length read does not consume reserve");
        std::vector<float> immediate_pcm, reserved_pcm;
        unsigned immediate_gaps = 0, reserved_gaps = 0, startup_frames = 0;
        bool immediate_started = false, reserved_started = false;
        for (uint32_t tick = 0; tick < 32; ++tick) {
            if (tick < 12) {
                immediate.push(make_frame(tick + 13, 1, .2f));
                reserved.push(make_frame(tick + 13, 1, .2f));
            }
            // 静音首块瞬时完成；有声块多用六帧，模拟 120 ms 的计算等待。
            if (tick == 18) {
                CHECK(immediate.try_process_one(immediate_pipe) && reserved.try_process_one(reserved_pipe),
                      "delayed producer finishes the second block");
            }
            const auto read = [&](StreamingRvc& stream, std::vector<float>& pcm, bool& started,
                                  unsigned& gaps, unsigned* startup) {
                const size_t got = stream.pop_output(output, MOZART_OUTPUT_SAMPLES);
                if (got) {
                    started = true;
                    pcm.insert(pcm.end(), output, output + got);
                } else if (!started) {
                    if (startup) ++*startup;
                } else if (pcm.size() < 2 * 11520) {
                    ++gaps;
                }
            };
            read(immediate, immediate_pcm, immediate_started, immediate_gaps, nullptr);
            read(reserved, reserved_pcm, reserved_started, reserved_gaps, &startup_frames);
        }
        CHECK(immediate_gaps == 6, "unbuffered path exposes the bounded jitter");
        CHECK(reserved_gaps == 0 && startup_frames == 8, "400 ms startup budget absorbs silence-to-speech compute jitter");
        CHECK(reserved_pcm == immediate_pcm && reserved_pcm.size() == 23040,
              "reserve preserves every emitted PCM sample and its order");
        for (uint32_t i = 1000; i < 1012; ++i) {
            reserved.push(make_frame(i, 1, .3f));
            CHECK(reserved.pop_output(output, MOZART_OUTPUT_SAMPLES) == 0, "reset waits for its input-time budget");
        }
        CHECK(reserved.try_process_one(reserved_pipe), "stream produces output after discontinuity");
        for (int i = 0; i < 8; ++i) {
            CHECK(reserved.pop_output(output, MOZART_OUTPUT_SAMPLES) == 0, "reset restores the startup reserve");
        }
        CHECK(reserved.pop_output(output, MOZART_OUTPUT_SAMPLES) == MOZART_OUTPUT_SAMPLES,
              "reset stream resumes after its reserve");
        buffered.startup_delay_out = 1;
        bool invalid = false;
        try { StreamingRvc rejected(buffered); }
        catch (const std::invalid_argument&) { invalid = true; }
        CHECK(invalid, "reserve rejects incomplete contract frames");
    }

    // ---- 用例 8：full-history 超过历史容量后不永久停摆 ----
    printf("test_full_history_capacity_reanchor\n");
    {
        StreamingRvc::Config cfg;
        cfg.skip_silence = false;
        cfg.full_history = true;
        cfg.window_samples = 32000;
        cfg.crossfade_out = 2880;
        cfg.right_context_samples = 0;
        cfg.guard_samples = 0;
        cfg.max_history_samples = 96000; // 允许 3 块后超容一次
        StreamingRvc s(cfg);
        Upsample3Pipeline pipe;

        uint32_t idx = 1;
        for (size_t frame = 0; frame < 400; ++frame) {
            s.push(make_frame(idx++, 1, 0.1f));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(s.stats().blocks.load() >= 3,
              "stream keeps producing after history capacity is exceeded");
        CHECK(s.stats().history_resets.load() >= 1,
              "history capacity triggers a re-anchor");
        const auto out = drain(s);
        CHECK(!out.empty(), "post-reanchor output present");
    }

    printf("test_segment_changes_preserve_pcm_timeline\n");
    {
        StreamingRvc s{StreamingRvc::Config{}};
        Upsample3Pipeline pipe;
        for (uint32_t i = 1; i <= 300; ++i) {
            s.push(make_frame(i, 1, .1f, static_cast<uint8_t>(i % 7), i * 20000000ull));
            while (s.try_process_one(pipe)) {}
        }
        CHECK(s.stats().resets.load() == 0, "utterance boundaries preserve rolling context");
        CHECK(s.stats().blocks.load() == 3, "continuous PCM still produces scheduled blocks");
        s.push(make_frame(301, 1, .1f, 1, 1000000ull));
        CHECK(s.stats().resets.load() == 1, "backward clock triggers reset");
        s.push(make_frame(302, 1, .1f, 1, 2000000000ull));
        CHECK(s.stats().resets.load() == 2, "large forward clock jump triggers reset");
    }


    printf("test_quality_vad_uses_target_history\n");
    for (const bool catch_up : {false, true}) {
        StreamingRvc::Config cfg;
        cfg.full_history = true;
        cfg.right_context_samples = 32000;
        cfg.guard_samples = 80;
        StreamingRvc stream(cfg);
        Upsample3Pipeline pipeline;
        std::vector<float> input;
        for (uint32_t i = 1; i <= 300; ++i) {
            const bool speech = i >= 131 && i <= 180;
            const auto frame = make_frame(i, speech, speech ? .25f : 0.f);
            input.insert(input.end(), frame.pcm, frame.pcm + MOZART_INPUT_SAMPLES);
            stream.push(frame);
            if (!catch_up) while (stream.try_process_one(pipeline)) {}
        }
        while (stream.try_process_one(pipeline)) {}
        const auto output = drain(stream);
        CHECK(output.size() == 2 * 93120, "two historical output hops are preserved");
        bool exact = output.size() == 2 * 93120;
        for (size_t i = 0; i < output.size(); ++i) exact = exact && std::abs(output[i] - input[i / 3]) < 1e-6f;
        CHECK(exact, "lookahead and backlog preserve the target speech samples");
    }

    printf("test_reset_rejects_all_old_inference_outcomes\n");
    for (int outcome = 0; outcome < 3; ++outcome) {
        class BlockedPipeline : public Upsample3Pipeline {
        public:
            explicit BlockedPipeline(int result) : outcome(result) {}
            std::promise<void> entered, release;
            std::shared_future<void> gate = release.get_future().share();
            int outcome;
            bool first = true;
            std::vector<float> process(const std::vector<float>& input) override {
                if (first) {
                    first = false;
                    entered.set_value();
                    if (gate.wait_for(std::chrono::seconds(3)) != std::future_status::ready)
                        throw std::runtime_error("test gate timed out");
                    if (outcome == 0) throw std::runtime_error("injected engine failure");
                    if (outcome == 1) return {};
                }
                return Upsample3Pipeline::process(input);
            }
        } pipeline(outcome);
        StreamingRvc::Config cfg;
        cfg.window_samples = 3840;
        cfg.crossfade_out = 2400;
        cfg.upstream_realtime = true;
        cfg.skip_silence = false;
        StreamingRvc stream(cfg);
        for (uint32_t i = 1; i <= 12; ++i) stream.push(make_frame(i, 1, .2f));
        auto inference = std::async(std::launch::async, [&] { return stream.try_process_one(pipeline); });
        CHECK(pipeline.entered.get_future().wait_for(std::chrono::seconds(2)) == std::future_status::ready,
              "inference reaches the reset barrier");
        stream.push(make_frame(1000, 1, .3f));
        pipeline.release.set_value();
        inference.get();
        CHECK(stream.output_pending() == 0, "old success, short output and failure cannot refill the reset stream");
        for (uint32_t i = 1001; i < 1012; ++i) stream.push(make_frame(i, 1, .3f));
        CHECK(stream.try_process_one(pipeline), "the new stream resumes after every old outcome");
        const auto output = drain(stream);
        CHECK(output.size() == 11520, "only the new output block is retained");
    }

    printf("test_output_metadata_tracks_samples_and_overruns\n");
    {
        StreamingRvc::Config cfg;
        cfg.skip_silence = false;
        StreamingRvc stream(cfg);
        Upsample3Pipeline pipeline;
        constexpr uint64_t base = 10000000000ull;
        for (uint32_t i = 1; i <= 1200; ++i) {
            stream.push(make_frame(i, 1, static_cast<float>(i), 1, base + i * 20000000ull));
            while (stream.try_process_one(pipeline)) {}
        }
        CHECK(stream.stats().output_overruns.load() > 0, "the metadata test exercises output eviction");
        float samples[480];
        for (int i = 0; i < 4; ++i) {
            mozart_frame_meta_t source{};
            CHECK(stream.pop_output(samples, 480, &source) == 480, "partial output reads remain available");
            CHECK(source.frame_idx == static_cast<uint32_t>(std::lround(samples[0])),
                  "eviction and partial reads preserve source frame ownership");
            CHECK(source.pts_ns >= base + source.frame_idx * 20000000ull
                  && source.pts_ns < base + (source.frame_idx + 1) * 20000000ull,
                  "source PTS stays within the owning capture frame");
        }
        stream.push(make_frame(4000, 1, .5f, 1, base + 4000 * 20000000ull));
        mozart_frame_meta_t empty{};
        empty.pts_ns = base;
        CHECK(stream.pop_output(samples, 480, &empty) == 0 && empty.pts_ns == 0,
              "reset clears queued source metadata");
    }


    printf("test_sola_overlap_preserves_previous_tail_pts\n");
    {
        constexpr size_t offset = 240;
        class ShiftedPipeline : public Upsample3Pipeline {
        public:
            size_t calls = 0;
            static float signal(int64_t sample) {
                uint32_t value = static_cast<uint32_t>(sample) * 1664525u + 1013904223u;
                value ^= value >> 13;
                return static_cast<float>(value % 1000) / 1000.f;
            }
            std::vector<float> process_realtime(const std::vector<float>&, const RvcRealtimeRequest& request) override {
                std::vector<float> output(request.output_samples);
                for (size_t i = 0; i < output.size(); ++i) {
                    output[i] = signal(static_cast<int64_t>(i) + (calls ? 11520 - 240 : 0));
                }
                ++calls;
                return output;
            }
        } pipeline;
        StreamingRvc::Config cfg;
        cfg.window_samples = 3840;
        cfg.crossfade_out = 2400;
        cfg.upstream_realtime = true;
        cfg.skip_silence = false;
        StreamingRvc stream(cfg);
        constexpr uint64_t base = 10000000000ull;
        for (uint32_t i = 1; i <= 12; ++i) stream.push(make_frame(i, 1, .2f, 1, base + i * 20000000ull));
        CHECK(stream.try_process_one(pipeline), "first SOLA block establishes its tail");
        drain(stream);
        for (uint32_t i = 13; i <= 24; ++i) stream.push(make_frame(i, 1, .2f, 1, base + i * 20000000ull));
        CHECK(stream.try_process_one(pipeline), "shifted block completes SOLA alignment");
        std::vector<float> overlap(cfg.crossfade_out);
        mozart_frame_meta_t meta{};
        CHECK(stream.pop_output(overlap.data(), overlap.size(), &meta) == overlap.size(), "overlap is available");
        CHECK(std::abs(overlap.front() - ShiftedPipeline::signal(11520)) < 1e-6f,
              "the overlap starts with the previous tail sample");
        CHECK(meta.pts_ns == base + 200000000ull, "overlap PTS follows the previous tail");
        float output[MOZART_OUTPUT_SAMPLES];
        stream.pop_output(output, MOZART_OUTPUT_SAMPLES, &meta);
        CHECK(std::abs(output[0] - ShiftedPipeline::signal(11520 + static_cast<int64_t>(cfg.crossfade_out))) < 1e-6f,
              "SOLA selects the nonzero alignment offset");
        CHECK(meta.pts_ns == base + 250000000ull + offset * 1000000000ull / MOZART_OUTPUT_SAMPLE_RATE,
              "post-overlap PTS follows the shifted source");
    }

    if (g_fail) {
        printf("\n%d test(s) FAILED\n", g_fail);
        return 1;
    }
    printf("\nall tests passed\n");
    return 0;
}
