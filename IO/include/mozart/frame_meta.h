// frame_meta.h — 16B 帧元数据 + 三类契约帧（C-ABI，唯一定义源）
// ============================================================================
// 本头文件是 Project Mozart 全项目 mozart_frame_meta_t / mozart_raw_frame_t /
// mozart_input_frame_t / mozart_output_frame_t 的唯一定义源。
//
// preprocessor/mozart.h 与 rvc-backend 的 FrameMeta 均改为 #include 本头，
// 消除三处各自定义的同步漂移风险。
//
// 采样率约定：
//   RAW    = 48 kHz / 960 samples / 20 ms  — 物理采集原始帧（PipeWire 输出）
//   INPUT  = 16 kHz / 320 samples / 20 ms  — 预处理输出 / 后处理输入契约帧
//   OUTPUT = 48 kHz / 960 samples / 20 ms  — 后处理变声输出帧
//
// 所有结构体严格 1 字节对齐，跨语言（C / C++ / Rust）ABI 稳定。
#ifndef MOZART_FRAME_META_H
#define MOZART_FRAME_META_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ---- 编译期断言宏（兼容 C11 _Static_assert 与 C++11 static_assert）---------
#ifdef __cplusplus
  #define MOZART_STATIC_ASSERT(cond, msg) static_assert(cond, msg)
#else
  #define MOZART_STATIC_ASSERT(cond, msg) _Static_assert(cond, msg)
#endif

// ---- 采样率与帧长常量 --------------------------------------------------------
#define MOZART_RAW_SAMPLE_RATE     48000
#define MOZART_RAW_FRAME_MS        20
#define MOZART_RAW_SAMPLES         960   // 48000 * 0.02

#define MOZART_INPUT_SAMPLE_RATE   16000
#define MOZART_INPUT_FRAME_MS      20
#define MOZART_INPUT_SAMPLES       320   // 16000 * 0.02

#define MOZART_OUTPUT_SAMPLE_RATE  48000
#define MOZART_OUTPUT_FRAME_MS     20
#define MOZART_OUTPUT_SAMPLES      960   // 48000 * 0.02

#define MOZART_CHANNELS            1

// ---- 16 字节帧元数据 ----------------------------------------------------------
// wire 布局（小端）：
//   offset 0..7   pts_ns      (u64)
//   offset 8..11  frame_idx   (u32)
//   offset 12     vad_flag    (u8)
//   offset 13     energy_db   (u8)
//   offset 14     conf        (u8)
//   offset 15     segment_id  (u8)
// 与 rvc-backend UDP MZRT 包头（去掉 4B magic 后）完全一致。
#pragma pack(push, 1)
typedef struct {
    uint64_t pts_ns;       // 演示时间戳（纳秒），由 IO 模块在采集时填充
    uint32_t frame_idx;    // 单调递增帧序号，由 IO 模块在采集时填充
    uint8_t  vad_flag;     // 语音激活: 0 = 静音, 1 = 有声（预处理填写）
    uint8_t  energy_db;    // 当前帧能量 (dB, 0-255)（预处理填写）
    uint8_t  conf;         // 降噪置信度 (0-255)（预处理填写）
    uint8_t  segment_id;   // 声音分段 ID (0 = 静音间隔)（预处理填写）
} mozart_frame_meta_t;
#pragma pack(pop)
// 静态断言：严格 16 字节 / 帧大小（见下方各结构体）
MOZART_STATIC_ASSERT(sizeof(mozart_frame_meta_t) == 16,
                     "mozart_frame_meta_t must be exactly 16 bytes");

// ---- 48kHz 原始采集帧（物理设备 → 预处理）-----------------------------------
#pragma pack(push, 1)
typedef struct {
    mozart_frame_meta_t meta;                    // 16B 元数据
    float               pcm[MOZART_RAW_SAMPLES]; // 960 点 float32 单声道 (3840 字节)
} mozart_raw_frame_t;
#pragma pack(pop)
MOZART_STATIC_ASSERT(sizeof(mozart_raw_frame_t) == 16 + 960 * 4,
                "mozart_raw_frame_t size mismatch");

// ---- 16kHz 输入契约帧（预处理 → 后处理）-------------------------------------
#pragma pack(push, 1)
typedef struct {
    mozart_frame_meta_t meta;                       // 16B 元数据（透传）
    float               pcm[MOZART_INPUT_SAMPLES];  // 320 点 float32 单声道 (1280 字节)
} mozart_input_frame_t;
#pragma pack(pop)
MOZART_STATIC_ASSERT(sizeof(mozart_input_frame_t) == 16 + 320 * 4,
                "mozart_input_frame_t size mismatch");

// ---- 48kHz 输出契约帧（后处理 → 物理输出/网络回传）--------------------------
#pragma pack(push, 1)
typedef struct {
    mozart_frame_meta_t meta;                        // 16B 元数据（透传）
    float               pcm[MOZART_OUTPUT_SAMPLES];  // 960 点 float32 单声道 (3840 字节)
} mozart_output_frame_t;
#pragma pack(pop)
MOZART_STATIC_ASSERT(sizeof(mozart_output_frame_t) == 16 + 960 * 4,
                "mozart_output_frame_t size mismatch");

// ---- UDP MZRT 契约包常量（与 rvc-backend 包格式对齐）-------------------------
#define MOZART_PACKET_MAGIC      0x4D5A5254u   // 'MZRT'
#define MOZART_PACKET_HEADER_SIZE 20           // 4B magic + 16B meta
// 输入包总大小: 20 + 320*4 = 1300 字节
// 输出包总大小: 20 + 960*4 = 3860 字节

#ifdef __cplusplus
}
#endif

#undef MOZART_STATIC_ASSERT

#endif // MOZART_FRAME_META_H
