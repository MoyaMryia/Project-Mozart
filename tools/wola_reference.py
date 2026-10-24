#!/usr/bin/env python3
"""WOLA 谱降噪参考实现与离线验证（M0）。

参考实现是 M1 预处理器 C 实现的对拍金标准：流式语义（逐帧状态机）、
参数与延迟口径在此钉死。验证电池输出噪声抑制、语音保持与音乐透明指标，
门限不达标时退出码非零。

用法：
  python3 tools/wola_reference.py --validate           # 跑验证电池并打印表格
  python3 tools/wola_reference.py --latency 128 64 1   # 实测指定配置的算法延迟
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile

SPEECH_SOURCES = [
    'rvc-golden/feature-latency-20261014/feature-latency-dsp-input.wav',
    'rvc-golden/full-run-20261005/natural-qiqi-realtime-trimmed.wav',
]

# 门限（计划 D1）：任一不达标记为 FAIL。
GATE_NR_DB = 12.0          # 稳态噪声抑制下限（纯噪声、排除首 1 秒收敛区）
GATE_SPEECH_SI_SDR_MIN = -1.0   # 纯语音经处理后的 SI-SDR 下限（透明护栏）
GATE_MUSIC_SI_SDR_MIN = 20.0    # 纯音乐 SI-SDR 下限（透明主口径）


@dataclass
class WolaConfig:
    frame: int = 128          # 分析窗长（样本，48 kHz）
    hop: int = 64             # 步进（50% 重叠）
    lookahead: int = 1        # 噪声估计前瞻帧数（0 = 纯因果）
    alpha_noise: float = 0.99 # 噪声通道慢 EMA（压帧级谱方差的关键）
    beta_dd: float = 0.98     # 决策导向先验 SNR 平滑
    noise_leak: float = 0.0004 # 噪声估计上爬速率（仅噪声主导 bin 生效）
    leak_gate: float = 3.0    # 上爬门控：参考谱/噪声 < 该值视为噪声主导
    floor_db: float = -18.0   # 增益地板
    noise_smooth_bins: int = 1 # 噪声参考谱的频域平滑半径（过大伤哼声邻 bin 语音）


class WolaSuppressor:
    """流式 WOLA 谱减降噪。push() 任意长度，返回等量输出（含算法延迟）。"""

    def __init__(self, config: WolaConfig, sample_rate: int = 48000):
        self.cfg = config
        n = config.frame
        self.window = np.sqrt(np.hanning(n + 1)[:n])  # 周期 sqrt-Hann
        self.bins = n // 2 + 1
        self.power_slow = None    # 噪声通道的慢 EMA 谱（状态不做累积频域平滑）
        self.noise = None         # 噪声谱估计
        self.in_buf = np.zeros(0)                    # 输入累积
        self.frame_queue: list[np.ndarray] = []      # 前瞻帧频谱队列
        self.ola = np.zeros(0)                       # 重叠叠加缓冲
        self.out_ready = np.zeros(0)                 # 已完成输出
        self.norm = np.zeros(0)                      # 窗平方归一缓冲
        self.xi_prev = None         # 决策导向的上一帧先验 SNR
        self.gain_prev = np.ones(self.bins)          # 上一帧增益
        self.init_frames = []       # 开局若干帧功率，取均值初始化（避免首帧抽样偏差）

    def _freq_smooth(self, spectrum: np.ndarray, radius: int) -> np.ndarray:
        """频域平滑（对副本做，不污染状态；两端复制延拓）。"""
        if radius <= 0:
            return spectrum
        kernel = np.ones(2 * radius + 1) / (2 * radius + 1)
        padded = np.concatenate([np.full(radius, spectrum[0]),
                                 spectrum,
                                 np.full(radius, spectrum[-1])])
        return np.convolve(padded, kernel, mode='same')[radius:-radius]

    # ---- 内部：更新估计与产出输出分离，前瞻在队列层实现 ----
    def _update_estimates(self, samples: np.ndarray) -> None:
        cfg = self.cfg
        spectrum = np.fft.rfft(self.window * samples)
        power = np.abs(spectrum) ** 2
        if self.power_slow is None:
            # 开局取前几帧功率均值初始化，抑制单帧抽样偏差造成的长收敛尾部。
            self.init_frames.append(power)
            if len(self.init_frames) >= 8:
                averaged = np.mean(self.init_frames, axis=0)
                self.power_slow = averaged.copy()
                self.noise = averaged.copy()
            self.frame_queue.append(spectrum)
            return
        else:
            self.power_slow = cfg.alpha_noise * self.power_slow \
                + (1 - cfg.alpha_noise) * power
        # 噪声 = 慢谱频域平滑后的低包络。上爬只在噪声主导的 bin 生效：
        # 持续音乐/语音的 bin 参考谱远高于噪声，门控关闭，估计不再爬升。
        reference = self._freq_smooth(self.power_slow, cfg.noise_smooth_bins)
        rise = reference < cfg.leak_gate * np.maximum(self.noise, 1e-20)
        proposed = np.where(rise, self.noise * (1.0 + cfg.noise_leak), self.noise)
        self.noise = np.minimum(proposed, reference)
        self.frame_queue.append(spectrum)

    def _emit_oldest(self) -> None:
        """决策导向维纳增益：先验 SNR 平滑使增益不随帧级谱方差抖动。"""
        cfg = self.cfg
        spectrum = self.frame_queue.pop(0)
        power = np.abs(spectrum) ** 2
        floor = 10 ** (cfg.floor_db / 20)
        post = power / np.maximum(self.noise, 1e-20)          # 后验 SNR
        if self.xi_prev is None:
            xi = np.maximum(post - 1.0, 0.0)
        else:
            # 标准决策导向：先验 = β·G²·post + (1-β)·max(post-1, 0)
            xi = cfg.beta_dd * self.gain_prev ** 2 * post \
                + (1 - cfg.beta_dd) * np.maximum(post - 1.0, 0.0)
        gain = np.maximum(xi / (1.0 + xi), floor)
        self.xi_prev = xi
        self.gain_prev = gain

        out_frame = np.fft.irfft(gain * spectrum, n=cfg.frame) * self.window
        need = cfg.frame
        if len(self.ola) < need:
            self.ola = np.concatenate([self.ola, np.zeros(need - len(self.ola))])
            self.norm = np.concatenate([self.norm, np.zeros(need - len(self.norm))])
        self.ola[:need] += out_frame
        self.norm[:need] += self.window * self.window

    def push(self, chunk: np.ndarray) -> np.ndarray:
        cfg = self.cfg
        self.in_buf = np.concatenate([self.in_buf, chunk])
        emitted = np.zeros(0)
        # 每凑满 frame + lookahead*hop 处理一帧：保证弹出的帧能看到 L 个未来帧。
        # 内层 while 在初始化完成后一次排干积压，避免初始化帧垫高驻留延迟。
        while len(self.in_buf) >= cfg.frame + cfg.lookahead * cfg.hop:
            self._update_estimates(self.in_buf[:cfg.frame])
            self.in_buf = self.in_buf[cfg.hop:]
            while self.noise is not None and len(self.frame_queue) > cfg.lookahead:
                self._emit_oldest()
                hop_out = self.ola[:cfg.hop] / np.maximum(self.norm[:cfg.hop], 1e-9)
                emitted = np.concatenate([emitted, hop_out])
                self.ola = self.ola[cfg.hop:]
                self.norm = self.norm[cfg.hop:]
        return emitted

    def flush(self) -> np.ndarray:
        """尾部补零排空队列中等待前瞻的帧。"""
        cfg = self.cfg
        tail = self.push(np.zeros(cfg.frame * 2 + cfg.lookahead * cfg.hop))
        while self.frame_queue:
            self._emit_oldest()
            hop_out = self.ola[:cfg.hop] / np.maximum(self.norm[:cfg.hop], 1e-9)
            tail = np.concatenate([tail, hop_out])
            self.ola = self.ola[cfg.hop:]
            self.norm = self.norm[cfg.hop:]
            if len(self.ola) < cfg.hop:
                break
        return tail


def process_array(x: np.ndarray, config: WolaConfig) -> np.ndarray:
    sup = WolaSuppressor(config)
    out = sup.push(x)
    out = np.concatenate([out, sup.flush()])
    return out


def measured_latency(config: WolaConfig) -> dict:
    """实测流式发射延迟：输出样本 n 的最终样值在输入消费到多少样本时才发出。

    WOLA 输出与输入索引对齐（重建无位移），延迟体现为发射时刻滞后，
    上限约为 frame + lookahead*hop。这里按发射轨迹统计最大与均值。
    """

    class Traced(WolaSuppressor):
        def __init__(self, cfg: WolaConfig):
            super().__init__(cfg)
            self.consumed = 0
            self.emitted = 0
            self.trace: list[tuple[int, int]] = []  # (输出段起点, 发射时已消费输入)

        def push(self, chunk: np.ndarray) -> np.ndarray:
            out = super().push(chunk)
            self.consumed += len(chunk)
            if len(out):
                self.trace.append((self.emitted, self.consumed))
                self.emitted += len(out)
            return out

    rng = np.random.default_rng(7)
    x = 0.1 * rng.standard_normal(48000)
    traced = Traced(config)
    # 按 128 样本（计划的 ALSA 周期）流式喂入，复现实时泵的发射节奏。
    # 统计只取稳态段；尾部排空的突发喂入不计入（连续流场景无此相位）。
    for start in range(0, len(x), 128):
        traced.push(x[start:start + 128])
    steady = [(start, consumed) for start, consumed in traced.trace if start >= 24000]
    traced.flush()
    # 负发射延迟表示索引错误：直接抛出异常终止。
    if any(consumed - start < 0 for start, consumed in steady):
        raise AssertionError('negative emission latency in steady trace')
    delays = [consumed - start for start, consumed in steady]
    return {'max_samples': int(max(delays)),
            'mean_samples': float(np.mean(delays)),
            'max_ms': max(delays) / 48.0,
            'align_offset_samples': 0}


# ---- 验证电池 ---------------------------------------------------------------

def load_mono_48k(path: str) -> np.ndarray:
    rate, data = wavfile.read(path)
    if data.dtype == np.int16:
        x = data.astype(np.float64) / 32768.0
    elif data.dtype == np.int32:
        x = data.astype(np.float64) / 2147483648.0
    else:
        x = data.astype(np.float64)
    if x.ndim == 2:
        x = x[:, 0]
    if rate != 48000:
        from scipy.signal import resample_poly
        x = resample_poly(x, 48000, rate)
    return x


def synth_noise(kind: str, n: int, rng: np.random.Generator) -> np.ndarray:
    if kind == 'white':
        return rng.standard_normal(n)
    if kind == 'pink':
        white = rng.standard_normal(n)
        from scipy.signal import lfilter
        b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
        a = [1, -2.494956002, 2.017265875, -0.522189400]
        return lfilter(b, a, white)
    if kind == 'hum50':
        t = np.arange(n) / 48000.0
        x = (np.sin(2 * np.pi * 50 * t) + 0.5 * np.sin(2 * np.pi * 150 * t)
             + 0.3 * np.sin(2 * np.pi * 250 * t))
        return x + 0.002 * rng.standard_normal(n)
    if kind == 'fan':
        white = rng.standard_normal(n)
        from scipy.signal import butter, lfilter
        b, a = butter(4, 900 / 24000)
        x = lfilter(b, a, white)
        t = np.arange(n) / 48000.0
        return x * (1 + 0.2 * np.sin(2 * np.pi * 4.7 * t))
    if kind == 'keyboard':
        x = np.zeros(n)
        for _ in range(n // 24000 + 1):
            pos = int(rng.integers(0, n - 500))
            burst = rng.standard_normal(500) * np.exp(-np.arange(500) / 60.0)
            x[pos:pos + 500] += burst
        return x
    raise ValueError(kind)


def synth_music(n: int, rng: np.random.Generator) -> np.ndarray:
    """合成音乐测试信号：和弦进行 + 谐波 + 颤音 + 包络。"""
    t_total = n / 48000.0
    chords = [[261.63, 329.63, 392.0], [349.23, 440.0, 523.25],
              [220.0, 261.63, 329.63], [440.0, 554.37, 659.26]]
    seg = int(1.2 * 48000)
    x = np.zeros(n)
    pos = 0
    idx = 0
    while pos < n:
        length = min(seg, n - pos)
        t = np.arange(length) / 48000.0
        tone = np.zeros(length)
        for f in chords[idx % len(chords)]:
            vib = 1 + 0.003 * np.sin(2 * np.pi * 5.0 * t + f)
            for h in range(1, 5):
                tone += (0.8 ** h) * np.sin(2 * np.pi * f * h * vib * t)
        env = np.minimum(1, np.arange(length) / 2000) * np.exp(-np.maximum(0, np.arange(length) - length + 2000) / 2000)
        x[pos:pos + length] += tone * env
        pos += length
        idx += 1
    return 0.2 * x / (np.max(np.abs(x)) + 1e-12)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x)) + 1e-20)


def si_sdr(reference: np.ndarray, estimate: np.ndarray) -> float:
    # 排空相位会让输出比输入长若⼲帧；统一按短端截断。
    length = min(len(reference), len(estimate))
    reference = reference[:length] - np.mean(reference[:length])
    estimate = estimate[:length] - np.mean(estimate[:length])
    alpha = np.dot(estimate, reference) / np.dot(reference, reference)
    target = alpha * reference
    noise = estimate - target
    return float(10 * np.log10(np.sum(target ** 2) / (np.sum(noise ** 2) + 1e-20) + 1e-20))


def lsd(x: np.ndarray, y: np.ndarray, frame: int = 1024) -> float:
    """对数谱距离（dB），逐帧均值。"""
    window = np.hanning(frame)
    distance = []
    for start in range(0, min(len(x), len(y)) - frame, frame // 2):
        sx = np.abs(np.fft.rfft(window * x[start:start + frame])) + 1e-10
        sy = np.abs(np.fft.rfft(window * y[start:start + frame])) + 1e-10
        d = 10 * np.log10(sx / sy)
        distance.append(np.sqrt(np.mean(d * d)))
    return float(np.mean(distance))


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    noise = noise[:len(speech)] if len(noise) >= len(speech) else np.pad(noise, (0, len(speech) - len(noise)))
    noise *= rms(speech) / (rms(noise) + 1e-20) / (10 ** (snr_db / 20))
    return speech + noise


def run_battery(config: WolaConfig, seconds: float = 8.0) -> dict:
    rng = np.random.default_rng(20261025)
    n = int(seconds * 48000)
    speeches = {}
    for path in SPEECH_SOURCES:
        if not Path(path).exists():
            print(f'语音源缺失：{path}', file=sys.stderr)
            return None
        x = load_mono_48k(path)[:n]
        x *= 0.1 / (rms(x) + 1e-20)
        speeches[Path(path).stem] = x
    music = synth_music(n, rng)
    results = {'latency': None, 'nr': [], 'speech': [], 'music': {}}

    latency = measured_latency(config)
    results['latency'] = latency
    offset = latency['align_offset_samples']

    noises = ['white', 'pink', 'hum50', 'fan', 'keyboard']
    for kind in noises:
        noise = synth_noise(kind, n, rng)
        for name, speech in speeches.items():
            for snr_db in (0, 5, 10, 15, 20):
                mix = mix_at_snr(speech, noise, snr_db)
                out = process_array(mix, config)
                nr_all = 10 * np.log10((np.mean(mix ** 2) + 1e-20) / (np.mean(out ** 2) + 1e-20))
                # SI-SDR：WOLA 重建与输入索引对齐（offset 恒 0），按短端截断
                ref = speech[:len(out) - offset]
                est = out[offset:]
                results['nr'].append({'noise': kind, 'speech': name, 'snr_db': snr_db,
                                      'nr_db': float(nr_all),
                                      'si_sdr_in': si_sdr(ref, (mix[:len(ref)])),
                                      'si_sdr_out': si_sdr(ref, est)})

    # 纯噪声抑制深度（无语音干扰，噪声估计可自由上爬）。
    # 稳态口径：排除首 1 秒收敛区。收敛段口径见报告适用范围。
    settle = 48000
    for kind in noises:
        noise = synth_noise(kind, n, rng) * 0.05
        out = process_array(noise, config)
        deep = out[offset + settle:]
        src = noise[settle:settle + len(deep)]
        results['nr'].append({'noise': kind, 'speech': '(noise-only)', 'snr_db': None,
                              'nr_db': float(10 * np.log10(np.mean(src ** 2) / (np.mean(deep ** 2) + 1e-20)))})

    # 纯语音透明护栏
    for name, speech in speeches.items():
        out = process_array(speech, config)
        ref = speech[:len(out) - offset]
        est = out[offset:]
        results['speech'].append({'speech': name, 'si_sdr_clean': si_sdr(ref, est)})

    # 音乐透明（干净音乐）：LSD 与 SI-SDR 双口径
    out = process_array(music, config)
    ref = music[:len(music) - offset]
    est = out[offset:]
    results['music']['lsd_clean_db'] = lsd(ref, est)
    results['music']['si_sdr_clean_db'] = si_sdr(ref, est)
    # 音乐+白噪 10 dB：报告值，无门限
    mix = mix_at_snr(music, synth_noise('white', n, rng), 10)
    out = process_array(mix, config)
    results['music']['lsd_white10_db'] = lsd(music[:len(music) - offset], out[offset:])
    return results


def evaluate_gates(results: dict) -> list[str]:
    failures = []
    stationary = {'white', 'pink', 'hum50', 'fan'}
    for row in results['nr']:
        if row['speech'] == '(noise-only)' and row['noise'] in stationary:
            if row['nr_db'] < GATE_NR_DB:
                failures.append(f"noise-only {row['noise']}: NR {row['nr_db']:.1f} < {GATE_NR_DB}")
        if row['snr_db'] is not None and row['noise'] in stationary and row['snr_db'] >= 15 \
                and row['speech'] != '(noise-only)':
            if row['si_sdr_out'] <= row['si_sdr_in']:
                failures.append(
                    f"{row['noise']}/{row['speech']}/snr{row['snr_db']}: "
                    f"SI-SDR out {row['si_sdr_out']:.1f} <= in {row['si_sdr_in']:.1f}")
    for row in results['speech']:
        if row['si_sdr_clean'] < GATE_SPEECH_SI_SDR_MIN:
            failures.append(f"clean speech {row['speech']}: SI-SDR {row['si_sdr_clean']:.1f}")
    if results['music']['si_sdr_clean_db'] < GATE_MUSIC_SI_SDR_MIN:
        failures.append(f"music SI-SDR {results['music']['si_sdr_clean_db']:.1f} < {GATE_MUSIC_SI_SDR_MIN}")
    return failures


def print_report(config: WolaConfig, results: dict) -> None:
    print(f"配置 frame={config.frame} hop={config.hop} lookahead={config.lookahead} "
          f"floor={config.floor_db}dB beta_dd={config.beta_dd} alpha_noise={config.alpha_noise}")
    latency = results['latency']
    print(f"实测发射延迟: max {latency['max_ms']:.2f} ms ({latency['max_samples']} 样本), "
          f"均值 {latency['mean_samples']:.0f} 样本, 对齐偏移 {latency['align_offset_samples']}")
    print("\n-- 纯噪声抑制深度 (dB) --")
    for row in results['nr']:
        if row['speech'] == '(noise-only)':
            print(f"  {row['noise']:<10} {row['nr_db']:6.1f}")
    print("\n-- 语音混合 (SI-SDR in -> out, dB) --")
    for row in results['nr']:
        if row['speech'] == '(noise-only)':
            continue
        print(f"  {row['noise']:<9} {row['speech'][:24]:<26} snr={row['snr_db']:>3} "
              f"{row['si_sdr_in']:6.1f} -> {row['si_sdr_out']:6.1f}")
    print("\n-- 纯语音透明护栏 --")
    for row in results['speech']:
        print(f"  {row['speech'][:36]:<38} SI-SDR {row['si_sdr_clean']:6.2f}")
    print(f"\n-- 音乐透明 -- LSD(干净) {results['music']['lsd_clean_db']:.2f} dB, "
          f"LSD(白噪10dB) {results['music']['lsd_white10_db']:.2f} dB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--frame', type=int, default=128)
    parser.add_argument('--hop', type=int, default=64)
    parser.add_argument('--lookahead', type=int, default=1)
    parser.add_argument('--json-out', type=Path, default=None)
    parser.add_argument('--latency', nargs=3, type=int, metavar=('FRAME', 'HOP', 'LOOKAHEAD'))
    arguments = parser.parse_args()

    config = WolaConfig(frame=arguments.frame, hop=arguments.hop,
                        lookahead=arguments.lookahead)
    if arguments.frame <= 0 or arguments.hop <= 0 or arguments.hop > arguments.frame \
            or arguments.lookahead < 0:
        parser.error('要求 0 < hop <= frame 且 lookahead >= 0')
    if arguments.latency:
        frame, hop, lookahead = arguments.latency
        if frame <= 0 or hop <= 0 or hop > frame or lookahead < 0:
            parser.error('要求 0 < hop <= frame 且 lookahead >= 0')
        latency = measured_latency(WolaConfig(frame, hop, lookahead))
        print(f"发射延迟: max {latency['max_ms']:.2f} ms ({latency['max_samples']} 样本), "
              f"均值 {latency['mean_samples']:.0f} 样本")
        return 0
    if not arguments.validate:
        parser.error('选择 --validate 或 --latency')

    results = run_battery(config)
    if results is None:
        return 1
    print_report(config, results)
    failures = evaluate_gates(results)
    if arguments.json_out:
        arguments.json_out.write_text(
            json.dumps({'config': vars(config), 'results': results,
                        'gate_failures': failures}, ensure_ascii=False, indent=1))
    if failures:
        print('\n门限未达标:', file=sys.stderr)
        for failure in failures:
            print(' -', failure, file=sys.stderr)
        return 1
    print('\n全部门限通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
