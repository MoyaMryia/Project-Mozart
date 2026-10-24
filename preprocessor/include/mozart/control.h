// control.h — mozart-pre 本地控制通道
// ============================================================================
// 绑定 127.0.0.1:<port> 的 UDP 文本协议，专线程阻塞收包：
//   mode ai|direct        切换播放路由（AI 回包 / 采集直出）
//   stage <name> on|off   name ∈ denoise|gate|agc|limiter
//   status                返回一行 JSON 状态快照
// 回调在控制线程执行，实现方只做原子写与加锁读，不做阻塞工作。
#ifndef MOZART_CONTROL_H
#define MOZART_CONTROL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_control mozart_control_t;

typedef struct {
    // route：0 = AI 回包播放，1 = 采集直出（由实现方定义枚举值）
    void (*set_route)(void *ctx, int route);
    // 返回 false 表示名字未知，控制层回错误应答。
    bool (*set_stage)(void *ctx, const char *name, bool on);
    // 把当前状态快照写入 buf（含结尾 0）；由实现方保证线程安全。
    void (*snapshot)(void *ctx, char *buf, size_t size);
} mozart_control_vt;

/** 打开并启动控制线程。port=0 或失败返回 NULL。 */
mozart_control_t *mozart_control_open(uint16_t port,
                                      const mozart_control_vt *vt,
                                      void *ctx);

/** 停止线程并释放。允许传 NULL。 */
void mozart_control_close(mozart_control_t *c);

#ifdef __cplusplus
}
#endif

#endif // MOZART_CONTROL_H
