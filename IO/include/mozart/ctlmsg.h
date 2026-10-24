// ctlmsg.h — Mozart 控制协议消息契约（M4，二进制 TLV）
// ============================================================================
// 三端共用（C 后端 / Python 桥 / TS 控制台）的唯一定义源；Python 侧由
// tools/gen_ctlmsg.py 读取本头生成常量，CI 比对生成物防漂移。
//
// 消息 = 定长头 8 B + 命令体（按 msg_type 查表）：
//   version u8（=1）
//   msg_type u8
//   flags   u16（bit0=request bit1=response bit2=event；其余 0）
//   req_id  u32（请求方分配，响应原样带回；事件为 0）
// 数值字段一律小端定宽；文本字段 = 长度前缀 u32 + UTF-8。
// 未知 msg_type：响应 ERR_UNKNOWN，不断链。
#ifndef MOZART_CTLMSG_H
#define MOZART_CTLMSG_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MOZART_CTL_VERSION 1
#define MOZART_CTL_HEADER_BYTES 8

// 消息类型（1..127 保留给核心面；0x1000+ 实验用途）
enum {
    MOZART_CTL_ERR = 1,          // 体：code u32 + 文本
    MOZART_CTL_PING = 2,         // 无体；响应 PONG
    MOZART_CTL_PONG = 3,
    MOZART_CTL_STATUS = 10,      // 无体；响应 STATUS_DATA
    MOZART_CTL_STATUS_DATA = 11, // 体：mode u8 + state u8 + 计数器组
    MOZART_CTL_MODE_SET = 20,    // 体：mode u8（0=idle 1=realtime 2=file）
    MOZART_CTL_MODE_EVT = 21,    // 体：mode u8 + state u8（主动推送）
    MOZART_CTL_SUBTITLE_EVT = 30 // 体：seq u32 + pts_ns u64 + is_final u8 + 文本
};

// 模式与状态枚举（与后端 ModeController 语义对齐）
enum {
    MOZART_CTL_MODE_IDLE = 0,
    MOZART_CTL_MODE_REALTIME = 1,
    MOZART_CTL_MODE_FILE = 2
};
enum {
    MOZART_CTL_STATE_DOWN = 0,
    MOZART_CTL_STATE_STARTING = 1,
    MOZART_CTL_STATE_READY = 2,
    MOZART_CTL_STATE_FAILED = 3
};

// 错误码
enum {
    MOZART_CTL_ERR_UNKNOWN_MSG = 1,
    MOZART_CTL_ERR_BAD_BODY = 2,
    MOZART_CTL_ERR_UNSUPPORTED = 3
};

#ifdef __cplusplus
}
#endif

#endif // MOZART_CTLMSG_H
