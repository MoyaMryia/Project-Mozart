#!/usr/bin/env python3
"""跨时钟域自适应重采样（Python 侧，两守护共用）。

调用方读取音频管道的积压，以采样帧为单位传入水位。
水位高时减少输出，水位低时增加输出。比例经过一阶平滑后用于线性插值。

帧 = CHANNELS 个交错 int16。纯标准库实现（守护进程运行环境无 numpy）。
"""
import struct

TARGET_BLOCKS = 4


class AsyncSrc:
    def __init__(self, channels=2, target_frames=4.0, ratio_max=0.0005):
        if target_frames <= 0 or ratio_max <= 0 or ratio_max > 0.05:
            raise ValueError('invalid src config')
        self.channels = channels
        self.target = float(target_frames)
        self.ratio_max = ratio_max
        self.smooth = 1.0
        self.carried = None     # 上一帧（channels 个 int 的元组）
        self.phase = 0.0        # 浮点读位置的小数部分 ∈ [0,1)
        self.ratio_sum = 0.0
        self.ratio_count = 0

    def ratio(self, fill_frames):
        desired = 2.0 - fill_frames / self.target
        lo, hi = 1.0 - self.ratio_max, 1.0 + self.ratio_max
        return min(max(desired, lo), hi)

    def observe(self, payload: bytes) -> None:
        """按收到的帧长更新水位目标，兼容 20 ms 与 128 样本帧。"""
        frames = len(payload) // (self.channels * 2)
        if frames > 0 and self.target != float(frames * TARGET_BLOCKS):
            self.target = float(frames * TARGET_BLOCKS)

    def process(self, payload: bytes, ratio: float) -> bytes:
        """按 ratio 对交错 S16LE 帧序列插值，返回输出字节流。

        相位语义：pos 为浮点读位置（帧单位），每输出一帧 pos += 1/ratio；
        pos 跨过整数帧界时消费新输入帧。ratio>1 时输出多于输入。
        """
        frame_bytes = self.channels * 2
        count = len(payload) // frame_bytes
        values = struct.unpack('<%dh' % (count * self.channels), payload)
        out = []
        previous = self.carried
        pos = self.phase
        self.smooth += (ratio - self.smooth) / 64.0
        step = 1.0 / self.smooth
        for i in range(count):
            base = i * self.channels
            current = values[base:base + self.channels]
            if previous is None:
                # 首帧只入 carried（延迟一帧），相位从 0 起
                previous = current
                pos = 0.0
                continue
            while pos < 1.0:
                blended = [previous[ch] * (1.0 - pos) + current[ch] * pos
                           for ch in range(self.channels)]
                out.extend(int(round(v)) for v in blended)
                pos += step
            pos -= 1.0
            previous = current
        self.carried = previous
        self.phase = pos
        if out:
            self.ratio_sum += self.smooth
            self.ratio_count += 1
            return struct.pack('<%dh' % len(out), *out)
        return b''
