#!/usr/bin/env python3
"""在本机用 C 均衡与 FFmpeg 保时长变调生成同一输入的试听。"""
import argparse
import ctypes as C
import hashlib
import io
import json
import subprocess
import time
from math import gcd
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
RATE = 48000
BLOCK_SAMPLES = 64
SUBPROCESS_TIMEOUT_SECONDS = 120
PTR = C.POINTER(C.c_float)
EQ = {'flat': (0.0, 0.0), 'light': (-2.0, 2.0), 'strong': (-4.0, 4.0)}


def build_library(path, extra_sources=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'cc', '-std=c11', '-O3', '-shared', '-fPIC', '-fno-fast-math',
        '-ffp-contract=off', '-Ipreprocessor/include', '-IIO/include',
        'tools/voice_dsp_preview.c', *extra_sources, 'preprocessor/src/dsp.c',
        'preprocessor/src/noise_suppressor.c', 'preprocessor/src/stages.c',
        '-lm', '-o', str(path.resolve()),
    ], cwd=ROOT, check=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)
    return path


def load_library(path):
    lib = C.CDLL(str(path.resolve()))
    lib.voice_eq_new.argtypes = [C.c_uint] + [C.c_double] * 5
    lib.voice_eq_new.restype = C.c_void_p
    lib.voice_eq_process.argtypes = [C.c_void_p, PTR, PTR, C.c_size_t]
    lib.voice_eq_process.restype = None
    for name in ['voice_eq_reset', 'voice_eq_free']:
        getattr(lib, name).argtypes = [C.c_void_p]
        getattr(lib, name).restype = None
    lib.mozart_dsp_new.argtypes = [C.c_void_p]
    lib.mozart_dsp_new.restype = C.c_void_p
    lib.mozart_dsp_enhance.argtypes = [C.c_void_p, PTR, C.c_size_t, PTR, C.c_size_t]
    lib.mozart_dsp_enhance.restype = C.c_size_t
    lib.mozart_dsp_free.argtypes = [C.c_void_p]
    lib.mozart_dsp_free.restype = None
    return lib


def decode_wav(raw):
    rate, data = wavfile.read(io.BytesIO(raw))
    if data.ndim not in (1, 2) or data.size == 0 or not 8000 <= rate <= 192000:
        raise ValueError('Expected a nonempty audio WAV at 8–192 kHz')
    if data.ndim == 2:
        data = data[:, 0]
    if data.dtype.kind == 'i':
        audio = data.astype(np.float64) / (float(np.iinfo(data.dtype).max) + 1)
    elif data.dtype == np.uint8:
        audio = (data.astype(np.float64) - 128) / 128
    elif data.dtype.kind == 'f':
        audio = data.astype(np.float64)
    else:
        raise ValueError(f'Unsupported WAV dtype: {data.dtype}')
    if not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1:
        raise ValueError('Input samples must be finite and within [-1, 1]')
    divisor = gcd(int(rate), RATE)
    audio = resample_poly(audio, RATE // divisor, int(rate) // divisor)
    return np.ascontiguousarray(audio, dtype=np.float32), int(rate), len(data)


def denoise(lib, audio):
    state = lib.mozart_dsp_new(None)
    if not state:
        raise MemoryError('Native DSP allocation failed')
    padded = np.concatenate([audio, np.zeros(RATE // 4, np.float32)])
    buffer = np.empty(4096, np.float32)
    parts = []
    first_emit = None
    try:
        for start in range(0, len(padded), BLOCK_SAMPLES):
            chunk = padded[start:start + BLOCK_SAMPLES]
            count = lib.mozart_dsp_enhance(state, chunk.ctypes.data_as(PTR), len(chunk),
                                           buffer.ctypes.data_as(PTR), len(buffer))
            if count > len(buffer):
                raise RuntimeError('Native preprocessing failed')
            if count:
                if first_emit is None:
                    first_emit = start + len(chunk)
                parts.append(buffer[:count].copy())
    finally:
        lib.mozart_dsp_free(state)
    result = np.concatenate(parts)
    if len(result) < len(audio):
        raise RuntimeError('Preprocessor output does not cover the input')
    return np.ascontiguousarray(result[:len(audio)]), dict(
        processing_chunk_samples=BLOCK_SAMPLES, first_emit_input_samples=first_emit,
        first_emit_observation='input block end',
        observation_granularity_samples=BLOCK_SAMPLES,
        first_emit_input_ms=first_emit / RATE * 1000, offline_alignment_samples=0,
        input_tail_padding_samples=len(padded) - len(audio),
        discarded_output_tail_samples=len(result) - len(audio))


def pitch_preserve_duration(audio, shift):
    shifted_rate = round(RATE * 2 ** (shift / 12))
    filters = f'asetrate={shifted_rate},aresample={RATE},atempo={RATE / shifted_rate:.12f}'
    padded = np.concatenate([audio, np.zeros(RATE // 4, np.float32)])
    completed = subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'f32le',
        '-ar', str(RATE), '-ac', '1', '-i', 'pipe:0', '-af', filters,
        '-f', 'f32le', 'pipe:1',
    ], input=padded.astype('<f4').tobytes(), stdout=subprocess.PIPE,
       check=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)
    result = np.frombuffer(completed.stdout, dtype='<f4')
    if len(result) < len(audio):
        raise RuntimeError('Pitch filter output does not cover the input duration')
    return np.ascontiguousarray(result[:len(audio)]), dict(
        pitch_method='FFmpeg asetrate + aresample + atempo', pitch_filters=filters,
        effective_pitch_ratio=shifted_rate / RATE, input_tail_padding_samples=RATE // 4,
        discarded_output_tail_samples=len(result) - len(audio),
        live_buffering_measured=False)


def render(lib, audio, preset, shift):
    low, presence = EQ[preset]
    state = lib.voice_eq_new(RATE, 250.0, low, 2500.0, presence, 0.7)
    if not state:
        raise MemoryError('Voice effect allocation failed')
    result = np.empty_like(audio)
    began = time.perf_counter()
    try:
        lib.voice_eq_process(state, audio.ctypes.data_as(PTR), result.ctypes.data_as(PTR), len(audio))
    finally:
        lib.voice_eq_free(state)
    details = {}
    if shift:
        result, details = pitch_preserve_duration(result, shift)
    return result, dict(eq_low_db=low, eq_presence_db=presence, semitones=shift,
                        render_seconds=time.perf_counter() - began, **details)


def write_audio(path, audio):
    if not np.isfinite(audio).all() or np.max(np.abs(audio)) >= 1:
        raise RuntimeError(f'Output is non-finite or would clip: {path.name}')
    wavfile.write(path, RATE, audio.astype(np.float32))
    return dict(samples=len(audio), rate=RATE, seconds=len(audio) / RATE,
                rms=float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))),
                peak=float(np.max(np.abs(audio))),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def listening_file(path, items):
    chunks = []
    for number, (label, audio) in enumerate(items):
        label_path = path.parent / f'label-{path.stem}-{number}.wav'
        subprocess.run(['espeak-ng', '-v', 'en-us', '-a', '20', '-s', '145',
                        '-w', str(label_path), label], check=True,
                       timeout=SUBPROCESS_TIMEOUT_SECONDS)
        spoken, _, _ = decode_wav(label_path.read_bytes())
        chunks.extend([spoken, np.zeros(RATE, np.float32), audio,
                       np.zeros(2 * RATE, np.float32)])
    return write_audio(path, np.concatenate(chunks))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path, help='new directory for listening WAVs')
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    audio, source_rate, source_samples = decode_wav(raw)
    args.output.mkdir(parents=True)
    library = build_library(ROOT / 'build/voice-preview/libvoice_preview.so')
    lib = load_library(library)
    began = time.perf_counter()
    base, preprocessing_details = denoise(lib, audio)
    preprocessing_seconds = time.perf_counter() - began
    outputs = {'01-original.wav': audio, '02-denoised.wav': base}
    report = dict(input=str(args.input.resolve()), input_sha256=hashlib.sha256(raw).hexdigest(),
                  source_rate=source_rate, source_samples=source_samples,
                  processing_rate=RATE, input_channel_policy='first channel',
                  gain_normalization=False, preprocessing=preprocessing_details,
                  preprocessing_seconds=preprocessing_seconds, outputs={})
    (args.output / '01-original.wav').write_bytes(raw)
    report['outputs']['02-denoised.wav'] = write_audio(args.output / '02-denoised.wav', base)
    variants = [
        ('03-clarity-light.wav', 'light', 0),
        ('04-clarity-strong.wav', 'strong', 0),
        ('05-pitch-up-5.wav', 'flat', 5),
        ('06-pitch-down-5.wav', 'flat', -5),
        ('07-clarity-pitch-up-5.wav', 'light', 5),
    ]
    for name, preset, shift in variants:
        result, stats = render(lib, base, preset, shift)
        if len(result) != len(audio):
            raise RuntimeError('Effect changed output sample count')
        outputs[name] = result
        report['outputs'][name] = {**stats, **write_audio(args.output / name, result)}
    report['listening'] = {}
    for name, names, labels in [
        ('00-clarity-comparison.wav', ['01-original.wav', '02-denoised.wav',
          '03-clarity-light.wav', '04-clarity-strong.wav'],
         ['Original input.', 'Noise suppression only.', 'Light clarity EQ.', 'Stronger clarity EQ.']),
        ('10-pitch-comparison.wav', ['02-denoised.wav', '05-pitch-up-5.wav',
          '06-pitch-down-5.wav', '07-clarity-pitch-up-5.wav'],
         ['Noise suppression only.', 'Pitch up five semitones.',
          'Pitch down five semitones.', 'Clarity EQ and pitch up.']),
    ]:
        report['listening'][name] = listening_file(args.output / name,
                                                  list(zip(labels, [outputs[n] for n in names])))
    (args.output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'output': str(args.output.resolve()), 'input_sha256': report['input_sha256'],
                      'source_seconds': source_samples / source_rate,
                      'preprocessing_seconds': preprocessing_seconds,
                      'effects': {k: v['render_seconds'] for k, v in report['outputs'].items()
                                  if 'render_seconds' in v}}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
