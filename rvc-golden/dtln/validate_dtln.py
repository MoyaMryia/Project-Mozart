#!/usr/bin/env python3
"""M5 第一步：DTLN ONNX 链流式验证（上游官方口径）。

契约与 real_time_processing_onnx.py 一致：16 kHz 固定、block_len 512、
block_shift 128（75% 重叠）、magnitude 域 mask（model_1）→ 时域特征增益
（model_2）。输入音频由调用方重采样到 16k。
验证：稳态噪声抑制、语音 SI-SDR 改善、单核实时因子。
门限：NR > 10 dB、语音增益 > 0、RTF < 0.3（16k 分支预算）。
用法：python3 rvc-golden/dtln/validate_dtln.py [--seconds 8] [--json-out PATH]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
RATE = 16000
BLOCK_LEN = 512
BLOCK_SHIFT = 128


class DtlnChain:
    """上游 real_time_processing_onnx.py 的逐行移植。"""

    def __init__(self):
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.s1 = ort.InferenceSession(str(HERE / 'model_1.onnx'), opts,
                                       providers=['CPUExecutionProvider'])
        self.s2 = ort.InferenceSession(str(HERE / 'model_2.onnx'), opts,
                                       providers=['CPUExecutionProvider'])
        self.in_names_1 = [i.name for i in self.s1.get_inputs()]
        self.in_names_2 = [i.name for i in self.s2.get_inputs()]
        self.in_buffer = np.zeros(BLOCK_LEN, dtype=np.float32)
        self.out_buffer = np.zeros(BLOCK_LEN, dtype=np.float32)
        self.states_1 = np.zeros([1, 2, 128, 2], dtype=np.float32)
        self.states_2 = np.zeros([1, 2, 128, 2], dtype=np.float32)

    def process(self, chunk: np.ndarray) -> np.ndarray:
        out = np.zeros(0, dtype=np.float32)
        for idx in range(len(chunk) // BLOCK_SHIFT):
            self.in_buffer[:-BLOCK_SHIFT] = self.in_buffer[BLOCK_SHIFT:]
            self.in_buffer[-BLOCK_SHIFT:] = chunk[idx * BLOCK_SHIFT:(idx + 1) * BLOCK_SHIFT]
            spec = np.fft.rfft(self.in_buffer)
            mag = np.reshape(np.abs(spec), (1, 1, -1)).astype(np.float32)
            phase = np.angle(spec)
            out_mask, self.states_1 = self.s1.run(None, {
                self.in_names_1[0]: mag, self.in_names_1[1]: self.states_1})
            estimated = mag * out_mask * np.exp(1j * phase)
            block = np.reshape(np.fft.irfft(estimated), (1, 1, -1)).astype(np.float32)
            out_block, self.states_2 = self.s2.run(None, {
                self.in_names_2[0]: block, self.in_names_2[1]: self.states_2})
            self.out_buffer[:-BLOCK_SHIFT] = self.out_buffer[BLOCK_SHIFT:]
            self.out_buffer[-BLOCK_SHIFT:] = 0
            self.out_buffer += np.squeeze(out_block)
            out = np.concatenate([out, self.out_buffer[:BLOCK_SHIFT]])
        return out


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-20)


def si_sdr(ref, est):
    ref = ref - np.mean(ref)
    est = est - np.mean(est)
    a = np.dot(est, ref) / np.dot(ref, ref)
    return float(10 * np.log10(np.sum((a * ref) ** 2) / (np.sum((est - a * ref) ** 2) + 1e-20)))


def synth_voice(n, rng):
    t = np.arange(n) / RATE
    x = np.zeros(n)
    env = (np.sin(2 * np.pi * 0.9 * t) > 0.2).astype(float)
    env = np.convolve(env, np.ones(1600) / 1600, mode='same')
    for f in (140, 280, 420, 560):
        x += (1.0 / (f / 140)) * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))
    return 0.2 * x * env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=8.0)
    parser.add_argument('--json-out', type=Path, default=HERE / 'report.json')
    arguments = parser.parse_args()
    rng = np.random.default_rng(2026)
    n = int(arguments.seconds * RATE)
    voice = synth_voice(n, rng)
    noise = 0.06 * rng.standard_normal(n)
    mix = (noise + voice).astype(np.float32)

    chain = DtlnChain()
    out = np.zeros(0, dtype=np.float32)
    t0 = time.monotonic()
    for start in range(0, n - BLOCK_SHIFT, BLOCK_SHIFT):
        out = np.concatenate([out, chain.process(mix[start:start + BLOCK_SHIFT])])
    elapsed = time.monotonic() - t0
    rtf = elapsed / arguments.seconds

    # 流式输出滞后 block_len-block_shift（首个填充窗），对齐后再取稳态段。
    latency = BLOCK_LEN - BLOCK_SHIFT
    span = min(len(out) - latency, n)
    est = out[latency:latency + span]
    ref = mix[:span]
    settle = RATE // 2
    nr_db = 10 * np.log10(rms(ref[settle:]) / (rms(est[settle:]) + 1e-20))
    sdr_in = si_sdr(voice[settle:span], ref[settle:])
    sdr_out = si_sdr(voice[settle:span], est[settle:])
    # 纯噪声对照（噪声抑制的稳态口径，与混合总量比区分开）
    noise_only = (0.06 * rng.standard_normal(n)).astype(np.float32)
    chain2 = DtlnChain()
    nout = np.zeros(0, dtype=np.float32)
    for start in range(0, n - BLOCK_SHIFT, BLOCK_SHIFT):
        nout = np.concatenate([nout, chain2.process(noise_only[start:start + BLOCK_SHIFT])])
    nspan = min(len(nout) - latency, n)
    ndeep = nout[latency:latency + nspan]
    nsrc = noise_only[:nspan]
    nr_only = 10 * np.log10(rms(nsrc[settle:]) / (rms(ndeep[settle:]) + 1e-20))
    report = {
        'stationary_noise_pure_db': round(nr_only, 1),
        'contract': {'rate': RATE, 'block_len': BLOCK_LEN, 'block_shift': BLOCK_SHIFT,
                     'overlap': '75%'},
        'stationary_noise_nr_db': round(nr_db, 1),
        'voice_si_sdr_in': round(sdr_in, 1),
        'voice_si_sdr_out': round(sdr_out, 1),
        'voice_gain_db': round(sdr_out - sdr_in, 1),
        'rtf_1core': round(rtf, 3),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    arguments.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    ok = (nr_only > 10 and sdr_out - sdr_in > 0 and rtf < 0.3)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
