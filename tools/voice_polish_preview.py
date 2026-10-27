#!/usr/bin/env python3
"""在本机用 C 算子为同一段人声生成音色美化试听。"""
import argparse
import ctypes as C
import hashlib
import json
import subprocess
import time
from math import gcd
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly, welch

ROOT = Path(__file__).resolve().parents[1]
RATE = 48000
BLOCK_SAMPLES = 64
SUBPROCESS_TIMEOUT_SECONDS = 120
PTR = C.POINTER(C.c_float)

CONFIG_NAMES = ('highpass_hz low_peak_hz low_peak_db low_peak_q mid_peak_hz '
                'mid_peak_db mid_peak_q presence_hz presence_db presence_q '
                'air_hz air_db room_ms room_mix '
                'gated_presence_db gated_air_db gate_ratio').split()

PROFILES = {
    '02-natural': dict(highpass_hz=0.0, low_peak_hz=220.0, low_peak_db=-3.5, low_peak_q=1.0,
                       mid_peak_hz=500.0, mid_peak_db=-1.5, mid_peak_q=1.2,
                       presence_hz=3000.0, presence_db=3.5, presence_q=0.8,
                       air_hz=8000.0, air_db=2.0, room_ms=0.0, room_mix=0.0,
                       gated_presence_db=0.0, gated_air_db=0.0, gate_ratio=4.0),
    '03-bright': dict(highpass_hz=0.0, low_peak_hz=220.0, low_peak_db=-4.5, low_peak_q=1.0,
                      mid_peak_hz=500.0, mid_peak_db=-2.0, mid_peak_q=1.2,
                      presence_hz=3200.0, presence_db=5.0, presence_q=0.8,
                      air_hz=8000.0, air_db=3.0, room_ms=0.0, room_mix=0.0,
                      gated_presence_db=0.0, gated_air_db=0.0, gate_ratio=4.0),
    '04-bright-room': dict(highpass_hz=0.0, low_peak_hz=220.0, low_peak_db=-4.5, low_peak_q=1.0,
                           mid_peak_hz=500.0, mid_peak_db=-2.0, mid_peak_q=1.2,
                           presence_hz=3200.0, presence_db=5.0, presence_q=0.8,
                           air_hz=8000.0, air_db=3.0, room_ms=23.0, room_mix=0.12,
                           gated_presence_db=0.0, gated_air_db=0.0, gate_ratio=4.0),
    '05-extreme-demo': dict(highpass_hz=0.0, low_peak_hz=220.0, low_peak_db=-10.0, low_peak_q=0.8,
                            mid_peak_hz=450.0, mid_peak_db=-5.0, mid_peak_q=1.0,
                            presence_hz=3000.0, presence_db=9.0, presence_q=0.7,
                            air_hz=8000.0, air_db=5.0, room_ms=0.0, room_mix=0.0,
                            gated_presence_db=0.0, gated_air_db=0.0, gate_ratio=4.0),
    '06-extreme-gated': dict(highpass_hz=0.0, low_peak_hz=220.0, low_peak_db=-10.0, low_peak_q=0.8,
                             mid_peak_hz=450.0, mid_peak_db=-5.0, mid_peak_q=1.0,
                             presence_hz=3000.0, presence_db=0.0, presence_q=0.7,
                             air_hz=8000.0, air_db=0.0, room_ms=0.0, room_mix=0.0,
                             gated_presence_db=9.0, gated_air_db=5.0, gate_ratio=2.0),
}
LEVEL_MATCH_EXEMPT = frozenset({'06-extreme-gated'})
AB_SEGMENT_SECONDS = 2.5
AB_START_SECONDS = 4.0
AB_REPEATS = 3
BANDS = (63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250,
         1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500)


class Config(C.Structure):
    _fields_ = [(name, C.c_double) for name in CONFIG_NAMES]


def build_library(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'cc', '-std=c11', '-O3', '-shared', '-fPIC', '-fno-fast-math',
        '-ffp-contract=off', '-Ipreprocessor/include', '-IIO/include',
        'tools/voice_polish.c', 'preprocessor/src/stages.c',
        '-lm', '-o', str(path.resolve()),
    ], cwd=ROOT, check=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)
    return path


def load_library(path):
    lib = C.CDLL(str(path.resolve()))
    lib.polish_new.argtypes = [C.c_uint, C.POINTER(Config)]
    lib.polish_new.restype = C.c_void_p
    lib.polish_process.argtypes = [C.c_void_p, PTR, PTR, C.c_size_t]
    lib.polish_process.restype = None
    for name in ('polish_reset', 'polish_free'):
        getattr(lib, name).argtypes = [C.c_void_p]
        getattr(lib, name).restype = None
    lib.polish_config_size.argtypes = []
    lib.polish_config_size.restype = C.c_size_t
    lib.polish_highpass_q.argtypes = []
    lib.polish_highpass_q.restype = C.c_double
    lib.polish_limiter_ceiling.argtypes = [C.c_uint]
    lib.polish_limiter_ceiling.restype = C.c_double
    lib.polish_first_echo_samples.argtypes = [C.c_uint, C.c_double]
    lib.polish_first_echo_samples.restype = C.c_size_t
    if lib.polish_config_size() != C.sizeof(Config):
        raise RuntimeError('C config layout differs from the Python field order')
    return lib


def limiter_ceiling(lib):
    return float(lib.polish_limiter_ceiling(RATE))


def read_source(path):
    rate, pcm = wavfile.read(path)
    if pcm.dtype != np.int16 or pcm.ndim != 1 or rate not in (16000, RATE) or len(pcm) < rate:
        raise ValueError('Expected a mono PCM16 16 kHz or 48 kHz WAV of at least one second')
    audio = pcm.astype(np.float32) / 32768.0
    if rate != RATE:
        factor = gcd(rate, RATE)
        audio = resample_poly(audio, RATE // factor, rate // factor).astype(np.float32)
    return np.ascontiguousarray(audio), rate, int(pcm.size)


def process(lib, audio, settings):
    config = Config(*(settings[name] for name in CONFIG_NAMES))
    state = lib.polish_new(RATE, C.byref(config))
    if not state:
        raise MemoryError('Polish state allocation failed')
    output = np.empty_like(audio)
    blocks = (len(audio) + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES
    began = time.perf_counter()
    try:
        for block in range(blocks):
            start = block * BLOCK_SAMPLES
            count = min(BLOCK_SAMPLES, len(audio) - start)
            lib.polish_process(state, audio[start:start + count].ctypes.data_as(PTR),
                               output[start:start + count].ctypes.data_as(PTR), count)
    finally:
        lib.polish_free(state)
    return output, time.perf_counter() - began


def active_mask(audio):
    frame = RATE // 50
    frames = audio[:len(audio) // frame * frame].reshape(-1, frame)
    energy = np.mean(frames.astype(np.float64) ** 2, axis=1)
    levels = 10 * np.log10(np.maximum(energy, 1e-20))
    active = levels >= np.percentile(levels, 60)
    return np.pad(active.repeat(frame), (0, len(audio) - len(active) * frame))


def rms(audio):
    return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))


def a_weight_db(frequency):
    squared = np.square(frequency)
    ratio = (12194.0 ** 2 * squared ** 2) / (
        (squared + 20.6 ** 2)
        * np.sqrt((squared + 107.7 ** 2) * (squared + 737.9 ** 2))
        * (squared + 12194.0 ** 2))
    return 20.0 * np.log10(np.maximum(ratio, 1e-30)) + 2.0


def loudness(audio):
    frequency, power = welch(audio, RATE, nperseg=8192)
    weight = 10.0 ** (a_weight_db(np.maximum(frequency, 1e-3)) / 10.0)
    return float(np.sqrt(np.sum(power * weight) * (frequency[1] - frequency[0])))


def spectrum_db(audio, mask):
    frequency, power = welch(audio[mask], RATE, nperseg=8192)
    levels = np.array([10 * np.log10(power[(frequency >= hz / 2 ** (1 / 6)) &
                                           (frequency < hz * 2 ** (1 / 6))].mean() + 1e-30)
                       for hz in BANDS])
    return levels - levels[BANDS.index(500):BANDS.index(2000) + 1].mean()


def write_audio(path, samples):
    pcm = np.int16(np.clip(np.round(samples * 32768.0), -32768, 32767))
    wavfile.write(path, RATE, pcm)
    return dict(samples=int(samples.size), rate=RATE, seconds=samples.size / RATE,
                rms=rms(samples), peak=float(np.max(np.abs(samples))),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def listening(path, clips, gap_seconds=0.6):
    gap = np.zeros(round(gap_seconds * RATE), dtype=np.float32)
    pieces = []
    for _label, samples in clips:
        pieces.extend((samples, gap))
    return write_audio(path, np.concatenate(pieces))


def ab_alternating(path, dry, processed, gap_seconds=0.25):
    start = round(AB_START_SECONDS * RATE)
    length = round(AB_SEGMENT_SECONDS * RATE)
    gap = np.zeros(round(gap_seconds * RATE), dtype=np.float32)
    pieces = []
    for _ in range(AB_REPEATS):
        pieces.extend((dry[start:start + length], gap, processed[start:start + length], gap))
    return write_audio(path, np.concatenate(pieces))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path, help='new output directory')
    args = parser.parse_args(argv)
    source, source_rate, source_samples = read_source(args.input)
    mask = active_mask(source)
    target_loudness = loudness(source[mask])
    if target_loudness <= 0.0:
        raise ValueError('Input needs nonzero active-frame energy')
    args.output.mkdir(parents=True)
    lib = load_library(build_library(ROOT / 'build/voice-polish/polish.so'))
    ceiling = limiter_ceiling(lib)

    report = dict(input=str(args.input.resolve()),
                  input_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest(),
                  source_rate=source_rate, source_samples=source_samples,
                  processing_rate=RATE, resampled=source_rate != RATE,
                  block_samples=BLOCK_SAMPLES, limiter_ceiling=ceiling,
                  loudness_method='active-frame A-weighted level to dry input, gain applied before limiting',
                  dry=write_audio(args.output / '01-dry.wav', source),
                  profiles={}, spectrum_dry=dict(zip(BANDS, np.round(spectrum_db(source, mask), 2).tolist())))
    matched = {}
    for name, settings in PROFILES.items():
        probe, probe_seconds = process(lib, source, settings)
        gain = 1.0 if name in LEVEL_MATCH_EXEMPT else target_loudness / loudness(probe[mask])
        processed, render_seconds = process(lib, np.ascontiguousarray(source * gain, np.float32), settings)
        info = dict(settings=settings, gain=gain,
                    probe_seconds=probe_seconds, render_seconds=render_seconds,
                    total_seconds=probe_seconds + render_seconds,
                    room_samples=int(lib.polish_first_echo_samples(RATE, settings['room_ms'])),
                    **write_audio(args.output / f'{name}.wav', processed))
        changed = spectrum_db(processed, mask) - spectrum_db(source, mask)
        info['spectrum_change_db'] = dict(zip(BANDS, np.round(changed, 2).tolist()))
        info['active_rms'] = rms(processed[mask])
        info['peak_after_limiter'] = float(np.max(np.abs(processed)))
        report['profiles'][name] = info
        matched[name] = processed

    report['listening'] = listening(args.output / '00-comparison.wav',
                                    [('Dry input.', source)] + list(matched.items()))
    report['ab_alternating'] = {
        'segment_seconds': AB_SEGMENT_SECONDS, 'start_seconds': AB_START_SECONDS,
        'repeats': AB_REPEATS, 'files': {}}
    for name in PROFILES:
        report['ab_alternating']['files'][name] = ab_alternating(
            args.output / f'10-ab-{name}.wav', source, matched[name])
    (args.output / 'manifest.json').write_text(
        json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'output': str(args.output.resolve()),
                      'profiles': {k: {'gain': round(v['gain'], 4),
                                       'total_seconds': round(v['total_seconds'], 4),
                                       'room_samples': v['room_samples'],
                                       'rms': round(v['rms'], 5),
                                       'peak': round(v['peak'], 4)}
                                   for k, v in report['profiles'].items()},
                      'listening': report['listening']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
