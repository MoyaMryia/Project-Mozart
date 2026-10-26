"""usb-audio/src.py 自适应重采样的行为测试。

钉三条性质：跨块流式恒等（尾帧随下一块补出）、±500 ppm 漂移吸收、
比例钳位。C 侧同算法测试见 preprocessor/tests/test_async_src.c。
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'usb-audio'))
from src import AsyncSrc


def frames(count, channels=2):
    return np.tile(np.arange(count, dtype='<i2')[:, None], (1, channels)).tobytes()


class AsyncSrcTests(unittest.TestCase):
    def test_streaming_identity(self):
        src = AsyncSrc(channels=2)
        out = src.process(frames(64), 1.0) + src.process(frames(64), 1.0)
        values = np.frombuffer(out, dtype='<i2').reshape(-1, 2)[:, 0]
        self.assertTrue((values == np.arange(len(values)) % 64).all())

    def test_positive_drift_absorbed(self):
        # 平滑收敛需 ~64 块：先跑预热块，再在稳态段上测比例
        src = AsyncSrc(channels=2)
        signal = (np.sin(np.arange(48000 * 0.1, step=0.01) * 0.01) * 8000).astype('<i2')
        stereo = np.stack([signal, signal], axis=1)
        warm = 64 * 256
        for s in range(0, warm, 256):
            src.process(stereo[s:s + 256].tobytes(), 1.0005)
        total_in = total_out = 0
        for s in range(warm, len(stereo) - 256, 256):
            total_in += 256
            total_out += len(src.process(stereo[s:s + 256].tobytes(), 1.0005)) // 4
        self.assertAlmostEqual(total_out / total_in, 1.0005, delta=0.0002)

    def test_negative_drift_absorbed(self):
        src = AsyncSrc(channels=2)
        signal = (np.sin(np.arange(48000 * 0.1, step=0.01) * 0.01) * 8000).astype('<i2')
        stereo = np.stack([signal, signal], axis=1)
        warm = 64 * 256
        for s in range(0, warm, 256):
            src.process(stereo[s:s + 256].tobytes(), 0.9995)
        total_in = total_out = 0
        for s in range(warm, len(stereo) - 256, 256):
            total_in += 256
            total_out += len(src.process(stereo[s:s + 256].tobytes(), 0.9995)) // 4
        self.assertAlmostEqual(total_out / total_in, 0.9995, delta=0.0002)

    def test_ratio_clamped(self):
        src = AsyncSrc(channels=2, target_frames=4.0, ratio_max=0.0005)
        self.assertEqual(src.ratio(99.0), 0.9995)
        self.assertEqual(src.ratio(0.1), 1.0005)
        self.assertEqual(src.ratio(4.0), 1.0)

    def test_feedback_tracks_both_sink_clock_directions(self):
        payload = frames(128)
        for drift in (-0.0003, 0.0, 0.0003):
            with self.subTest(drift=drift):
                src = AsyncSrc(channels=2, target_frames=512)
                fill = 512.0
                total_out = 0
                for _ in range(3000):
                    output = src.process(payload, src.ratio(fill))
                    count = len(output) // 4
                    total_out += count
                    fill += count - 128 * (1.0 + drift)
                    self.assertGreater(fill, 508)
                    self.assertLess(fill, 516)
                self.assertAlmostEqual(total_out / (3000 * 128), 1.0 + drift, delta=0.00001)

    def test_ratio_changes_are_smoothed_before_resampling(self):
        src = AsyncSrc(channels=2)
        src.process(frames(128), 1.0005)
        self.assertAlmostEqual(src.smooth, 1.0 + 0.0005 / 64)
        self.assertAlmostEqual(src.ratio_sum / src.ratio_count, src.smooth)

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            AsyncSrc(channels=2, target_frames=0)
        with self.assertRaises(ValueError):
            AsyncSrc(channels=2, ratio_max=0.5)


if __name__ == '__main__':
    unittest.main()
