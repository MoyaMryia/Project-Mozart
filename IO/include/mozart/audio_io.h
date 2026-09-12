// audio_io.h — Project Mozart IO 子系统统一 C-ABI 接口
// ============================================================================
// 暴露两类能力：
//   1. 音频流生命周期（网络 UDP；PipeWire 物理流为 TODO 待办，见下方注释）
//   2. C++ SPSC 无锁环形队列（mozart/ring_buffer.hpp，IO 内部与测试使用）
//
// 实际消费情况（2026-09 现状）：
//   - rvc-backend 的实时数据面通过 mozart_io_create_udp_stream + read/write_frame
//     使用本 C-ABI（唯一生产消费者）；
//   - preprocessor (C11) 未链接本库，采集/播放走自带 ALSA（capture.c/playback.c）。
//
#ifndef MOZART_AUDIO_IO_H
#define MOZART_AUDIO_IO_H

#include <stdint.h>
#include <stdbool.h>
#include "mozart/frame_meta.h"

// ---- 符号导出宏 --------------------------------------------------------------
#if defined(MOZART_IO_STATIC)
    // 静态库或头文件包含场景：无需 dllexport
    #define MOZART_API
#elif defined(_WIN32)
    #if defined(MOZART_IO_EXPORTS)
        #define MOZART_API __declspec(dllexport)
    #else
        #define MOZART_API __declspec(dllimport)
    #endif
#else
    #if defined(MOZART_IO_EXPORTS)
        #define MOZART_API __attribute__((visibility("default")))
    #else
        #define MOZART_API
    #endif
#endif

#ifdef __cplusplus
extern "C" {
#endif

// ---- 流方向 ------------------------------------------------------------------
#define MOZART_IO_DIR_CAPTURE   0   // 采集端：ReadFrame 产出 raw_frame 或 input_frame
#define MOZART_IO_DIR_PLAYBACK  1   // 播放端：WriteFrame 消费 output_frame

// ---- 不透明句柄 --------------------------------------------------------------
typedef void* mozart_stream_handle_t;

// =============================================================================
// 1. 音频流工厂与生命周期
// =============================================================================

// TODO(pipewire): 物理硬件实时流（PipeWire 麦克风采集或虚拟源输出）为 TODO.md
// 待办，stub 已停用；真实驱动落地时恢复本声明与 audio_stream.cpp 中的工厂。
// MOZART_API mozart_stream_handle_t mozart_io_create_pipewire_stream(const char* device_name,
//                                                                    int direction);

// 实时网络 UDP 流 (定长 MZRT 契约包收发)
//   host:      本地绑定地址 (Capture) 或对端地址 (Playback)
//   port:      UDP 端口
//   direction: CAPTURE = 接收 (ReadFrame 期望 mozart_input_frame_t / 1296B)
//              PLAYBACK = 发送 (WriteFrame 期望 mozart_output_frame_t / 3856B，线包 3860B)
MOZART_API mozart_stream_handle_t mozart_io_create_udp_stream(const char* host,
                                                              uint16_t port,
                                                              int direction);

// Open and close are separate from construction so status_manager can keep a
// configured stream handle while switching its underlying resources on/off.
MOZART_API bool mozart_io_open_stream(mozart_stream_handle_t handle,
                                      uint32_t sample_rate,
                                      uint32_t frame_duration_ms,
                                      uint32_t ring_capacity);
MOZART_API void mozart_io_close_stream(mozart_stream_handle_t handle);
MOZART_API bool mozart_io_is_stream_open(mozart_stream_handle_t handle);

// Close and destroy the stream object. Passing NULL is safe.
MOZART_API void mozart_io_destroy_stream(mozart_stream_handle_t handle);

// 阻塞式读写 20ms 契约帧
//   buf_size 必须与具体流期望的帧大小一致（见上方工厂函数注释）
//   返回 false 表示流已关闭或 buf_size 校验失败
MOZART_API bool mozart_io_read_frame (mozart_stream_handle_t handle,
                                      void*       out_frame_buf,
                                      uint32_t    buf_size);
MOZART_API bool mozart_io_write_frame(mozart_stream_handle_t handle,
                                      const void* in_frame_buf,
                                      uint32_t    buf_size);

// 注释掉的 mozart_ring_* C-ABI（原 audio_io.h §2）：全仓库零调用者——
// udp_stream 内部直接用 C++ SpscRing，preprocessor 亦未接入。若未来
// preprocessor 需要跨 C 边界共享环形队列，从 git 历史恢复本段与
// ring_buffer.cpp 的桥接块。

#ifdef __cplusplus
}
#endif

#endif // MOZART_AUDIO_IO_H
