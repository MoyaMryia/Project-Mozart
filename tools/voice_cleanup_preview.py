#!/usr/bin/env python3
"""分析确认的 16 kHz 录音，并在本机生成轻量人声清理试听。"""
import argparse
import ctypes as C
import hashlib
import io
import json
import time
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile
from scipy.ndimage import uniform_filter1d

from voice_dsp_preview import (ROOT, RATE, PTR, build_library, load_library,
                               denoise, write_audio, listening_file)

# 与 preprocessor/src/noise_suppressor.c 的数值地板一致；低于它按零能量处理。
POWER_FLOOR = 1e-20
SOURCE_RATE = 16000
PCM16_SCALE = 32768.0
FRAME_SECONDS = 0.02
SOURCE_FRAME_SAMPLES = round(SOURCE_RATE * FRAME_SECONDS)
MIN_CALIBRATION_DYNAMIC_DB = 15.0
ACTIVE_OFFSET_DB = 15.0
ACTIVE_PEAK_MARGIN_DB = 22.0
TONE_WINDOW = 4096
TONE_HOP = 512
TONE_FFT = 16384
QUIET_PERCENTILE = 15
TONE_PROMINENCE_DB = 8.0
TONE_PERSISTENCE = 0.7
SPECTRAL_FRAME = 1024
SPECTRAL_HOP = 256
SPECTRAL_FLOOR_DB = -24.0
SPECTRAL_NOISE_SCALE = 3.0
SPECTRAL_ATTACK_MS = 1.0
SPECTRAL_RELEASE_MS = 20.0

CONFIG_NAMES = (
    'notch_1_hz notch_2_hz notch_3_hz notch_q lowpass_hz gate_open_db '
    'gate_close_db gate_floor_db gate_hold_ms gate_attack_ms gate_release_ms '
    'plosive_max_db plosive_ratio').split()
STAT_NAMES = ('samples gate_attenuated_samples plosive_attenuated_samples '
              'min_gate_gain min_plosive_gain max_abs_output').split()


class Config(C.Structure):
    _fields_ = [(name, C.c_double) for name in CONFIG_NAMES]


def bind_cleanup_api(lib):
    lib.cleanup_new.argtypes = [C.c_uint, C.POINTER(Config)]
    lib.cleanup_new.restype = C.c_void_p
    lib.cleanup_process.argtypes = [C.c_void_p, PTR, PTR, C.c_size_t]
    lib.cleanup_process.restype = None
    for name in ['cleanup_reset', 'cleanup_free']:
        getattr(lib, name).argtypes = [C.c_void_p]
        getattr(lib, name).restype = None
    lib.cleanup_get_stats.argtypes = [C.c_void_p, C.POINTER(C.c_double)]
    lib.cleanup_get_stats.restype = None
    return lib


def load_cleanup_library(path):
    return bind_cleanup_api(load_library(path))


def read_source(raw):
    rate, pcm = wavfile.read(io.BytesIO(raw))
    if rate != SOURCE_RATE or pcm.dtype != np.int16 or pcm.ndim != 1 or len(pcm) < 2 * rate:
        raise ValueError('Expected a mono PCM16 16 kHz WAV of at least two seconds')
    return pcm.astype(np.float64) / PCM16_SCALE


def levels_and_masks(audio):
    size = SOURCE_FRAME_SAMPLES
    frames = audio[:len(audio) // size * size].reshape(-1, size)
    power = np.mean(frames * frames, axis=1)
    levels = 10 * np.log10(np.maximum(power, POWER_FLOOR))
    valid = power > POWER_FLOOR
    if not valid.any():
        raise ValueError('Noise calibration needs nonzero audio')
    threshold = float(np.percentile(levels[valid], QUIET_PERCENTILE))
    if float(levels.max()) - threshold < MIN_CALIBRATION_DYNAMIC_DB:
        raise ValueError('Noise calibration needs identifiable low-energy intervals')
    quiet = valid & (levels <= threshold)
    active_threshold = max(threshold + ACTIVE_OFFSET_DB, float(levels.max()) - ACTIVE_PEAK_MARGIN_DB)
    active = levels >= active_threshold
    return levels, quiet, active, threshold, active_threshold


def diagnose(audio):
    levels, quiet, active, threshold, active_threshold = levels_and_masks(audio)
    frames = np.lib.stride_tricks.sliding_window_view(audio, TONE_WINDOW)[::TONE_HOP]
    window_power = np.mean(frames * frames, axis=1)
    long_quiet = (window_power > POWER_FLOOR) & (
        10 * np.log10(np.maximum(window_power, POWER_FLOOR)) <= threshold)
    decisions = []
    if long_quiet.any():
        freq, psd = signal.periodogram(frames[long_quiet], SOURCE_RATE,
                                      window='hann', nfft=TONE_FFT, axis=1)
        median = np.median(psd, axis=0)
        for low, high in [(45, 80), (90, 110), (190, 210)]:
            band = np.flatnonzero((freq >= low) & (freq <= high))
            index = band[np.argmax(median[band])]
            neighbors = (np.abs(freq - freq[index]) >= 8) & (np.abs(freq - freq[index]) <= 25)
            reference = np.median(psd[:, neighbors], axis=1)
            prominence = 10 * np.log10(np.maximum(psd[:, index], POWER_FLOOR)
                                       / np.maximum(reference, POWER_FLOOR))
            persistent = float(np.mean(prominence >= TONE_PROMINENCE_DB))
            evidence = persistent >= TONE_PERSISTENCE
            enabled = evidence and high < 150
            decisions.append(dict(frequency_hz=float(freq[index]),
                median_quiet_prominence_db=float(np.median(prominence)),
                quiet_window_fraction_above_threshold=persistent,
                persistent=evidence, notch_candidate=enabled,
                reason='confirmed low-band candidate' if enabled else
                       'preserve speech fundamental region' if evidence else 'insufficient persistence'))
    else:
        decisions.append(dict(notch_candidate=False, reason='no long quiet windows for tone analysis'))
    deviation = audio[1:-1] - (audio[:-2] + audio[2:]) * 0.5
    local = signal.medfilt(np.abs(deviation), 31)
    candidates = (np.abs(deviation) > 0.01) & (np.abs(deviation) > 12 * (local + 1 / PCM16_SCALE))
    candidates[:15] = candidates[-15:] = False
    peak = float(np.max(np.abs(audio)))
    flat = (audio[1:] == audio[:-1]) & (np.abs(audio[:-1]) >= 0.95 * peak)
    result = dict(source_samples=len(audio), analysis_frame_samples=SOURCE_FRAME_SAMPLES,
        complete_analysis_frames=len(levels), quiet_frames=int(quiet.sum()),
        active_frames=int(active.sum()), quiet_threshold_dbfs=threshold,
        quiet_median_dbfs=float(np.median(levels[quiet])),
        active_threshold_dbfs=active_threshold,
        peak_dbfs=20 * np.log10(peak),
        full_scale_samples=int(np.sum(np.abs(audio) >= (PCM16_SCALE - 1) / PCM16_SCALE)),
        repeated_near_peak_pairs=int(flat.sum()), isolated_spike_candidates=int(candidates.sum()),
        spike_interpolation_enabled=False, tone_decisions=decisions,
        tone_prominence_threshold_db=TONE_PROMINENCE_DB, tone_persistence_threshold=TONE_PERSISTENCE,
        long_quiet_windows=int(long_quiet.sum()), tone_window_samples=TONE_WINDOW,
        tone_hop_samples=TONE_HOP, tone_fft_samples=TONE_FFT,
        frame_selection_method='RMS threshold proxies; no manual speech labels',
        quiet_frames_are_verified_noise=False)
    return result, quiet, active


def native_clean(lib, audio, settings):
    config = Config(*(settings[name] for name in CONFIG_NAMES))
    state = lib.cleanup_new(RATE, C.byref(config))
    if not state:
        raise MemoryError('Cleanup state allocation failed')
    output = np.empty_like(audio)
    stats = (C.c_double * len(STAT_NAMES))()
    try:
        began = time.perf_counter()
        lib.cleanup_process(state, audio.ctypes.data_as(PTR), output.ctypes.data_as(PTR), len(audio))
        elapsed = time.perf_counter() - began
        lib.cleanup_get_stats(state, stats)
    finally:
        lib.cleanup_free(state)
    return output, dict(native_seconds=elapsed, settings=settings,
                        telemetry=dict(zip(STAT_NAMES, stats)))


def spectral_clean(audio, quiet):
    began = time.perf_counter()
    frequencies, times, spectrum = signal.stft(audio, RATE, nperseg=SPECTRAL_FRAME,
        noverlap=SPECTRAL_FRAME - SPECTRAL_HOP, boundary='zeros', padded=True)
    indices = np.floor(times / FRAME_SECONDS).astype(int)
    valid = indices < len(quiet)
    selected = np.zeros(len(times), bool)
    selected[valid] = quiet[indices[valid]]
    if selected.sum() < 8:
        raise ValueError('Noise spectrum calibration needs more low-energy frames')
    power = np.abs(spectrum) ** 2
    noise = np.median(power[:, selected], axis=1) * SPECTRAL_NOISE_SCALE
    floor = 10 ** (SPECTRAL_FLOOR_DB / 20)
    attack = np.exp(-SPECTRAL_HOP / (SPECTRAL_ATTACK_MS / 1000 * RATE))
    release = np.exp(-SPECTRAL_HOP / (SPECTRAL_RELEASE_MS / 1000 * RATE))
    gain = np.ones(len(frequencies))
    filtered = np.empty_like(spectrum)
    for frame in range(len(times)):
        target = np.maximum(1 - noise / np.maximum(power[:, frame], POWER_FLOOR), floor)
        target = uniform_filter1d(target, size=3, mode='nearest')
        smoothing = np.where(target > gain, attack, release)
        gain = smoothing * gain + (1 - smoothing) * target
        filtered[:, frame] = spectrum[:, frame] * gain
    _, reconstructed = signal.istft(filtered, RATE, nperseg=SPECTRAL_FRAME,
        noverlap=SPECTRAL_FRAME - SPECTRAL_HOP, boundary=True)
    return np.ascontiguousarray(reconstructed[:len(audio)], np.float32), dict(
        spectral_seconds=time.perf_counter() - began, analysis_window_samples=SPECTRAL_FRAME,
        hop_samples=SPECTRAL_HOP, noise_calibration_frames=int(selected.sum()),
        noise_floor_db=SPECTRAL_FLOOR_DB, noise_scale=SPECTRAL_NOISE_SCALE,
        gain_attack_ms=SPECTRAL_ATTACK_MS, gain_release_ms=SPECTRAL_RELEASE_MS,
        noise_profile_mode='whole-file low-energy proxy profile; offline',
        noise_profile_is_proxy=True, calibration_frames_speech_labels_available=False,
        centered_analysis=True, live_buffering_measured=False)


def source_masks(quiet, active, length):
    size = SOURCE_FRAME_SAMPLES * (RATE // SOURCE_RATE)
    count = len(quiet) * size
    return (np.pad(np.repeat(quiet, size), (0, length - count)),
            np.pad(np.repeat(active, size), (0, length - count)))


def quiet_core_regions(quiet):
    edges = np.diff(np.r_[False, quiet, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return [(int(start + 1), int(end - 1)) for start, end in zip(starts, ends)
            if end - start >= 3]


def rms(audio):
    return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))


def level_match(audio, active_mask, target):
    present = rms(audio[active_mask])
    gain = target / present
    return np.ascontiguousarray(audio * gain, np.float32), gain


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path, help='new output directory')
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    source = read_source(raw)
    diagnostics, quiet, active = diagnose(source)
    audio = np.ascontiguousarray(signal.resample_poly(source, RATE // SOURCE_RATE, 1), np.float32)
    quiet_mask, active_mask = source_masks(quiet, active, len(audio))
    args.output.mkdir(parents=True)
    library = build_library(ROOT / 'build/voice-cleanup/cleanup.so', ['tools/voice_cleanup.c'])
    lib = load_cleanup_library(library)
    base, preprocessing = denoise(lib, audio)
    notches = [r['frequency_hz'] for r in diagnostics['tone_decisions'] if r['notch_candidate']]
    notches = (notches + [0.0] * 3)[:3]
    common = dict(notch_1_hz=0, notch_2_hz=0, notch_3_hz=0,
        notch_q=30, gate_open_db=diagnostics['quiet_median_dbfs'] + 14,
        gate_close_db=diagnostics['quiet_median_dbfs'] + 8,
        gate_hold_ms=120, gate_attack_ms=3, gate_release_ms=100, plosive_ratio=4)
    gentle, gentle_details = native_clean(lib, base, dict(common, lowpass_hz=6800,
                                                          gate_floor_db=-12, plosive_max_db=-4))
    spectral, spectral_details = spectral_clean(base, quiet)
    strong, strong_details = native_clean(lib, spectral, dict(common, lowpass_hz=6200,
                                                              gate_floor_db=-18, plosive_max_db=-8))
    trial, trial_details = native_clean(lib, base, dict(common,
        notch_1_hz=notches[0], notch_2_hz=notches[1], notch_3_hz=notches[2],
        lowpass_hz=0, gate_floor_db=0, plosive_max_db=0))
    signals = {'01-original.wav': audio, '02-current-denoise.wav': base,
               '03-clean-gentle.wav': gentle, '04-clean-strong.wav': strong}
    (args.output / '01-original.wav').write_bytes(raw)
    report = dict(input=str(args.input.resolve()), input_sha256=hashlib.sha256(raw).hexdigest(),
        diagnostics=diagnostics, preprocessing=preprocessing,
        gentle=gentle_details, strong={**spectral_details, **strong_details},
        notch_trial=dict(**trial_details, used_in_main_variants=False,
            reason='diagnostic only; main profiles use soft gate and spectral gain'),
        raw_gain_normalization=False, matched_level_method='source-defined active-frame RMS',
        outputs={}, matched_outputs={})
    base_quiet = max(rms(base[quiet_mask]), np.sqrt(POWER_FLOOR))
    base_active = rms(base[active_mask])
    report['notch_trial']['quiet_change_db_vs_current'] = 20 * np.log10(
        max(rms(trial[quiet_mask]), np.sqrt(POWER_FLOOR)) / base_quiet)
    regions = []
    for start, end in quiet_core_regions(quiet):
        lo, hi = round(start * FRAME_SECONDS * RATE), round(end * FRAME_SECONDS * RATE)
        change = 20 * np.log10(max(rms(trial[lo:hi]), np.sqrt(POWER_FLOOR)) /
                               max(rms(base[lo:hi]), np.sqrt(POWER_FLOOR)))
        regions.append(dict(start_seconds=start * FRAME_SECONDS,
                            end_seconds=end * FRAME_SECONDS, level_change_db=change))
    report['notch_trial']['quiet_core_regions'] = regions
    for name, samples in signals.items():
        info = dict(quiet_rms_dbfs=20 * np.log10(max(rms(samples[quiet_mask]), np.sqrt(POWER_FLOOR))),
                    active_rms_dbfs=20 * np.log10(rms(samples[active_mask])))
        if name != '01-original.wav':
            info.update(write_audio(args.output / name, samples))
        report['outputs'][name] = info
    report['listening'] = {'raw': listening_file(args.output / '00-original-levels.wav', [
        ('Original input.', audio), ('Previous noise suppression.', base),
        ('Gentle cleanup.', gentle), ('Stronger spectral cleanup.', strong)])}
    matched = []
    for number, (name, samples) in enumerate(signals.items(), 11):
        adjusted, gain = level_match(samples, active_mask, base_active)
        filename = f'{number}-speech-matched-{name[3:]}'
        report['matched_outputs'][filename] = dict(gain=gain,
            active_rms=rms(adjusted[active_mask]),
            quiet_change_db_vs_current=20 * np.log10(max(rms(adjusted[quiet_mask]), np.sqrt(POWER_FLOOR)) / base_quiet),
            **write_audio(args.output / filename, adjusted))
        matched.append(adjusted)
    report['listening']['matched'] = listening_file(args.output / '10-speech-rms-matched.wav', [
        (label, samples) for label, samples in zip([
            'Original, speech level matched.', 'Previous processing, speech level matched.',
            'Gentle cleanup, speech level matched.', 'Stronger cleanup, speech level matched.'], matched)])
    report['processing_difference'] = dict(
        definition='current processing minus stronger output; includes gain and phase changes',
        **write_audio(args.output / '20-processing-difference.wav', base - strong))
    write_audio(args.output / '90-not-selected-notch-trial.wav', trial)
    (args.output / 'analysis.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'output':str(args.output.resolve()), 'diagnostics':diagnostics,
        'matched_metrics': {k: dict(quiet_change_db_vs_current=v['quiet_change_db_vs_current'], gain=v['gain'])
                            for k,v in report['matched_outputs'].items()},
        'native_seconds': [gentle_details['native_seconds'], strong_details['native_seconds']],
        'spectral_seconds': spectral_details['spectral_seconds']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
