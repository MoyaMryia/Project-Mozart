#!/usr/bin/env python3
"""跨时钟域自适应重采样（Python 侧，两守护共用）。

与 preprocessor/src/async_src.c 同算法：水位驱动比例 + 一阶平滑 +
线性插值。调用方监控 USB 入口队列水位（帧数），按比例插值消费——
水位高于目标时消费更慢（输出多于输入），低于时更快。

帧 = CHANNELS 个交错 int16。process 的输入以"上一帧"为左端点做逐帧补间。
"""
import numpy as np


class AsyncSrc:
    def __init__(self, channels=2, target_frames=4.0, ratio_max=0.0005):
        if target_frames <= 0 or ratio_max <= 0 or ratio_max > 0.05:
            raise ValueError('invalid src config')
        self.channels = channels
        self.target = float(target_frames)
        self.ratio_max = ratio_max
        self.smooth = 1.0
        self.carried = None     # 上一帧（长度 channels 的 float 数组）
        self.phase = 0.0        # 浮点读位置的小数部分 ∈ [0,1)
        self.ratio_sum = 0.0
        self.ratio_count = 0

    def ratio(self, fill_frames):
        desired = fill_frames / self.target
        lo, hi = 1.0 - self.ratio_max, 1.0 + self.ratio_max
        return min(max(desired, lo), hi)

    def process(self, payload: bytes, ratio: float) -> bytes:
        """按 ratio 对交错 S16LE 帧序列插值，返回输出字节流。

        相位语义：维护浮点读位置 pos（帧单位）。每输出一帧 pos += ratio；
        pos 跨过下一整数帧界时消费新输入帧。ratio==1 时输出与输入逐帧相同。
        """
        frames = np.frombuffer(payload, dtype='<i2').reshape(-1, self.channels)
        out_frames = []
        previous = self.carried
        pos = self.phase           # ∈ [0,1)：相对"下一输入帧"的补间位置
        for i in range(len(frames)):
            current = frames[i].astype(np.float64)
            if previous is None:
                # 首帧只入 carried（延迟一帧），相位从 0 起
                previous = current
                pos = 0.0
                continue
            step = 1.0 / ratio
            while pos < 1.0:
                out_frames.append(previous * (1.0 - pos) + current * pos)
                pos += step
            pos -= 1.0
            previous = current
        self.carried = previous
        self.phase = pos
        if out_frames:
            self.ratio_sum += ratio
            self.ratio_count += 1
            self.smooth += (ratio - self.smooth) / 64.0
        if not out_frames:
            return b''
        return np.stack(out_frames).astype('<i2').tobytes()
