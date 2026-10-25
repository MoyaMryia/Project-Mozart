// async_src.c — 水位驱动自适应重采样实现
// ============================================================================
// 比例控制：目标比例 = clamp(fill_ratio, 1±ratio_max)；
// 实际比例在其上再做一阶平滑（时间常数 ≈ 64 块），避免比例跳变产生副频。
// 重采样：线性插值，读指针为浮点相位；相位跨过输入尾部时由调用方补喂。
// 比例>1（水位高）时输出帧数多于输入（读得慢），<1 时少于输入。
#include "mozart/async_src.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

struct mozart_src {
    double target;
    double ratio_max;
    double smooth;      // 平滑后的比例
    double phase;       // 输入流上的浮点读位置（相对每次 process 的起点语义由调用方维护）
    double ratio_sum;
    long   ratio_count;
};

mozart_src_t *mozart_src_new(double target_frames, double ratio_max)
{
    if (target_frames <= 0 || ratio_max <= 0 || ratio_max > 0.05) return NULL;
    mozart_src_t *src = calloc(1, sizeof(*src));
    if (!src) return NULL;
    src->target = target_frames;
    src->ratio_max = ratio_max;
    src->smooth = 1.0;
    src->phase = 0.0;
    return src;
}

double mozart_src_ratio(const mozart_src_t *src, double fill_ratio)
{
    if (!src) return 1.0;
    double desired = fill_ratio;
    const double lo = 1.0 - src->ratio_max;
    const double hi = 1.0 + src->ratio_max;
    if (desired < lo) desired = lo;
    if (desired > hi) desired = hi;
    return desired;
}

int mozart_src_process(mozart_src_t *src, const float *in, int n,
                       float *out, int out_cap, double ratio)
{
    if (!src || !in || !out || n < 2 || ratio <= 0) return -1;
    // 平滑比例（每块向目标靠 1/64）
    src->smooth += (ratio - src->smooth) / 64.0;
    src->ratio_sum += src->smooth;
    src->ratio_count++;

    // 输入为一段连续流的窗口；相位在窗口内推进，尾差由调用方跨块衔接：
    // 约定：调用方每块把上一次剩余的"最后 1 帧"复制到本块开头（见 bridge 用法），
    // 本函数从 phase∈[0,1) 开始读，输出 floor 帧后把 phase 归一到 [0,1)。
    int written = 0;
    double pos = src->phase;         // 上一块遗留的帧内相位
    int i = 0;                       // 输入帧游标
    // 恒等快速路径：比例精确为 1 时逐帧直通（无插值损耗）。
    if (src->smooth == 1.0) {
        while (i < n && written < out_cap) {
            out[written++] = in[i];
            i++;
        }
        src->phase = 0.0;
        return written;
    }
    const double step = 1.0 / src->smooth;   // 水位高（ratio>1）→ 消费更慢
    while (i + 1 < n && written < out_cap) {
        // 线性插值输出一帧
        const float a = in[i], b = in[i + 1];
        out[written++] = (float)(a + (b - a) * pos);
        // 按比例推进读位置
        pos += step;
        while (pos >= 1.0) {
            pos -= 1.0;
            i++;
            if (i + 1 >= n) goto done;
        }
    }
done:
    src->phase = pos;
    return written;
}

double mozart_src_avg_ratio(const mozart_src_t *src)
{
    return (src && src->ratio_count) ? src->ratio_sum / src->ratio_count : 1.0;
}

void mozart_src_free(mozart_src_t *src)
{
    free(src);
}
