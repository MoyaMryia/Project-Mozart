// audio_io.h — Project Mozart IO 子系统统一 C-ABI 接口
// ============================================================================
// 暴露两类能力：
//   1. 音频流生命周期（物理 PipeWire / 网络 UDP / 文件 WAV / Mock）
//   2. SPSC 无锁环形队列（预分配、固定尺寸有界拷贝）
//
// 设计目标：preprocessor (C11) 与 rvc-backend (C++17) 均通过本头文件 +
// libmozart_io 链接，把所有 IO 收敛到 IO 模块；status_manager 编排模式
// 切换时只对本接口下命令，不再穿透两个业务模块。
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
typedef void* mozart_ring_handle_t;

// =============================================================================
// 1. 音频流工厂与生命周期
// =============================================================================

// 物理硬件实时流 (PipeWire 麦克风采集或虚拟源输出)
//   device_name: 设备标识 (如 "default", "mozart_mic")；NULL = 默认设备
//   direction:   MOZART_IO_DIR_CAPTURE = 采集 (ReadFrame 期望 mozart_raw_frame_t / 3856B)
//                MOZART_IO_DIR_PLAYBACK = 播放 (WriteFrame 期望 mozart_output_frame_t / 3856B)
MOZART_API mozart_stream_handle_t mozart_io_create_pipewire_stream(const char* device_name,
                                                                   int direction);

// 实时网络 UDP 流 (定长 MZRT 契约包收发)
//   host:      本地绑定地址 (Capture) 或对端地址 (Playback)
//   port:      UDP 端口
//   direction: CAPTURE = 接收 (ReadFrame 期望 mozart_input_frame_t / 1296B)
//              PLAYBACK = 发送 (WriteFrame 期望 mozart_output_frame_t / 3860B)
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

// 查询流底层延迟（纳秒）；不支持时返回 0
MOZART_API uint64_t mozart_io_get_underlying_latency_ns(mozart_stream_handle_t handle);

// =============================================================================
// 2. SPSC 无锁环形队列（预分配、固定尺寸有界拷贝）
// =============================================================================

// 创建无锁单写单读环形队列
//   capacity: 可容纳的最大帧数（建议 2 的幂，本实现内部向上取整到 2 的幂）
//   item_size: 每帧字节数
MOZART_API mozart_ring_handle_t mozart_ring_create(uint32_t capacity, uint32_t item_size);

// 销毁队列并释放内存
MOZART_API void mozart_ring_destroy(mozart_ring_handle_t ring);

// 生产者线程写入一帧（队满返回 false，不覆盖）
MOZART_API bool mozart_ring_push(mozart_ring_handle_t ring, const void* data);

// 消费者线程读出一帧（队空返回 false）
MOZART_API bool mozart_ring_pop(mozart_ring_handle_t ring, void* out_data);

// 当前可读帧数（消费者侧观察值，用于丢帧追赶判定）
MOZART_API uint32_t mozart_ring_get_readable_count(mozart_ring_handle_t ring);

// 队列容量
MOZART_API uint32_t mozart_ring_capacity(mozart_ring_handle_t ring);

#ifdef __cplusplus
}
#endif

#endif // MOZART_AUDIO_IO_H
