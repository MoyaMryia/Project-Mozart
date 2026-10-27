"""WOLA 参考实现的回归测试。

钉死四条性质：流式与整块一致（C 移植的合同）、确定性、透明性、
稳态噪声抑制与发射延迟上界。指标门限的完整电池见
tools/wola_reference.py --validate 与 reports/wola-m0-20261025/。
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wola_reference import (GATE_NR_DB, NS_INIT_FRAMES, WolaConfig, WolaSuppressor,
                            measured_latency, process_array, rms, synth_music)


class WolaReferenceTests(unittest.TestCase):
    def config(self):
        return WolaConfig(frame=128, hop=64, lookahead=0)

    def sine(self, seconds=1.0, freq=440.0, level=0.1):
        n = int(seconds * 48000)
        t = np.arange(n) / 48000.0
        return level * np.sin(2 * np.pi * freq * t)

    def test_streaming_matches_block(self):
        """任意分块喂入与整块处理输出一致——流式合同，C 移植对拍的依据。"""
        rng = np.random.default_rng(11)
        x = 0.1 * rng.standard_normal(48000)
        whole = process_array(x, self.config())
        sup = WolaSuppressor(self.config())
        parts = []
        position = 0
        for size in [7, 251, 64, 1000, 128, 96]:
            parts.append(sup.push(x[position:position + size]))
            position += size
        while position < len(x):
            take = min(333, len(x) - position)
            parts.append(sup.push(x[position:position + take]))
            position += take
        parts.append(sup.flush())
        chunked = np.concatenate(parts)
        length = min(len(chunked), len(whole))
        self.assertEqual(length, len(whole))
        self.assertLess(float(np.max(np.abs(chunked[:length] - whole[:length]))), 1e-9)

    def test_deterministic(self):
        rng = np.random.default_rng(12)
        x = 0.1 * rng.standard_normal(24000)
        first = process_array(x, self.config())
        second = process_array(x, self.config())
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_dynamic_music_transparent(self):
        """动态音乐（和弦进行）不被削弱；增益地板只影响间隙。"""
        rng = np.random.default_rng(21)
        x = synth_music(48000 * 2, rng)
        out = process_array(x, self.config())
        length = min(len(x), len(out))
        ratio_db = 20 * np.log10(rms(out[:length]) / rms(x[:length]))
        self.assertGreater(ratio_db, -3.0)

    def test_stationary_tone_suppressed(self):
        """恒定单音与窄带哼声在估计器内同类，按设计被抑制。"""
        x = self.sine(seconds=2.0)
        out = process_array(x, self.config())
        settle = 48000
        deep = out[settle:]
        src = x[settle:settle + len(deep)]
        depth = 10 * np.log10(np.mean(src ** 2) / np.mean(deep ** 2))
        self.assertGreater(depth, 10.0)

    def test_white_noise_suppressed(self):
        rng = np.random.default_rng(13)
        x = 0.05 * rng.standard_normal(48000 * 2)
        out = process_array(x, self.config())
        settle = 48000
        deep = out[settle:]
        src = x[settle:settle + len(deep)]
        depth = 10 * np.log10(np.mean(src ** 2) / np.mean(deep ** 2))
        self.assertGreater(depth, 10.0)

    def test_silence_prefix_then_white_noise_suppressed(self):
        rng = np.random.default_rng(131)
        noise = 0.05 * rng.standard_normal(3 * 48000)
        prefix = np.zeros(48000)
        x = np.concatenate([prefix, noise])
        out = process_array(x, self.config())
        start = len(prefix) + 48000
        end = min(len(out), len(x))
        source = x[start:end]
        result = out[start:end]
        nr_db = 10 * np.log10(np.mean(source ** 2) / (np.mean(result ** 2) + 1e-30))
        self.assertGreaterEqual(nr_db, GATE_NR_DB)

    def test_noise_returns_after_silence(self):
        rng = np.random.default_rng(133)
        initial = 0.05 * rng.standard_normal(48000)
        noise = 0.05 * rng.standard_normal(3 * 48000)
        x = np.concatenate([initial, np.zeros(8 * 48000), noise])
        out = process_array(x, self.config())
        start = 10 * 48000
        end = min(len(out), len(x))
        source, result = x[start:end], out[start:end]
        nr_db = 10 * np.log10(np.mean(source ** 2) / np.mean(result ** 2))
        self.assertGreaterEqual(nr_db, GATE_NR_DB)

    def test_long_silence_is_finite_zero_and_aligned(self):
        silence_samples = NS_INIT_FRAMES * self.config().frame * 64
        x = np.zeros(silence_samples)
        out = process_array(x, self.config())
        self.assertGreaterEqual(len(out), len(x))
        self.assertLessEqual(len(out), len(x) + 256)
        self.assertTrue(np.isfinite(out).all())
        self.assertTrue(np.all(out == 0.0))

    def test_silence_prefix_variable_chunks_match_whole(self):
        rng = np.random.default_rng(132)
        x = np.concatenate([np.zeros(48000), 0.05 * rng.standard_normal(48000)])
        whole = process_array(x, self.config())
        sup = WolaSuppressor(self.config())
        parts = []
        position = 0
        sizes = [1, 63, 128, 701, 17, 2048]
        while position < len(x):
            take = min(sizes[position % len(sizes)], len(x) - position)
            parts.append(sup.push(x[position:position + take]))
            position += take
        parts.append(sup.flush())
        chunked = np.concatenate(parts)
        self.assertEqual(len(chunked), len(whole))
        self.assertLess(float(np.max(np.abs(chunked - whole))), 1e-9)

    def test_latency_bound(self):
        """稳态发射延迟不超过 frame + lookahead*hop + 一个喂入周期。"""
        config = self.config()
        latency = measured_latency(config)
        bound = config.frame + config.lookahead * config.hop + 128
        self.assertLessEqual(latency['max_samples'], bound)

    def test_output_length_covers_input(self):
        x = self.sine(seconds=0.5)
        out = process_array(x, self.config())
        self.assertGreaterEqual(len(out), len(x))


if __name__ == '__main__':
    unittest.main()
