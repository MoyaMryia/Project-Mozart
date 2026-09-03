#!/usr/bin/env python3
"""Compare streaming audition WAVs against a locked offline Golden WAV."""

import argparse
import json

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


SAMPLE_RATE = 16_000
FEATURE_HOP = 160


def correlation(left, right):
    left_std = float(np.std(left))
    right_std = float(np.std(right))
    if left_std < 1e-12 or right_std < 1e-12:
        return 1.0 if np.array_equal(left, right) else 0.0
    return float(np.corrcoef(left, right)[0, 1])


def optional_mean(values):
    return float(np.mean(values)) if values.size else None


def load_audio(path):
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        divisor = np.gcd(sample_rate, SAMPLE_RATE)
        audio = resample_poly(
            audio, SAMPLE_RATE // divisor, sample_rate // divisor
        )
    return np.asarray(audio, dtype=np.float32)


def extract_features(audio):
    mel = librosa.feature.melspectrogram(
        y=audio,
        sr=SAMPLE_RATE,
        n_fft=1024,
        hop_length=FEATURE_HOP,
        n_mels=80,
        fmin=40,
        fmax=7600,
        power=2.0,
    )
    log_mel = librosa.power_to_db(mel, ref=1.0, top_db=80)
    rms = librosa.feature.rms(
        y=audio, frame_length=1024, hop_length=FEATURE_HOP
    )[0]
    centroid = librosa.feature.spectral_centroid(
        y=audio, sr=SAMPLE_RATE, n_fft=1024, hop_length=FEATURE_HOP
    )[0]
    pitch = librosa.pyin(
        audio,
        fmin=55,
        fmax=600,
        sr=SAMPLE_RATE,
        frame_length=1024,
        hop_length=FEATURE_HOP,
    )[0]
    return log_mel, rms, centroid, pitch


def aligned_slices(length, lag):
    if lag >= 0:
        return slice(lag, length), slice(0, length - lag)
    return slice(0, length + lag), slice(-lag, length)


def compare(reference_path, candidate_path, stream_hop):
    reference_info = sf.info(reference_path)
    candidate_info = sf.info(candidate_path)
    reference_duration = reference_info.frames / reference_info.samplerate
    candidate_duration = candidate_info.frames / candidate_info.samplerate
    reference = load_audio(reference_path)
    candidate = load_audio(candidate_path)
    samples = min(reference.size, candidate.size)
    reference, candidate = reference[:samples], candidate[:samples]
    reference_features = extract_features(reference)
    candidate_features = extract_features(candidate)
    frames = min(reference_features[0].shape[1], candidate_features[0].shape[1])

    best_alignment = None
    max_lag = min(20, frames - 1)
    for lag in range(-max_lag, max_lag + 1):
        reference_slice, candidate_slice = aligned_slices(frames, lag)
        reference_mel = reference_features[0][:, reference_slice].ravel()
        candidate_mel = candidate_features[0][:, candidate_slice].ravel()
        mel_correlation = correlation(reference_mel, candidate_mel)
        if (
            best_alignment is None
            or mel_correlation > best_alignment[0] + 1e-12
            or (
                abs(mel_correlation - best_alignment[0]) <= 1e-12
                and abs(lag) < abs(best_alignment[1])
            )
        ):
            best_alignment = mel_correlation, lag, reference_slice, candidate_slice

    mel_correlation, lag, reference_slice, candidate_slice = best_alignment
    reference_mel = reference_features[0][:, reference_slice]
    candidate_mel = candidate_features[0][:, candidate_slice]
    reference_rms = reference_features[1][reference_slice]
    candidate_rms = candidate_features[1][candidate_slice]
    reference_centroid = reference_features[2][reference_slice]
    candidate_centroid = candidate_features[2][candidate_slice]
    reference_pitch = reference_features[3][reference_slice]
    candidate_pitch = candidate_features[3][candidate_slice]
    both_voiced = np.isfinite(reference_pitch) & np.isfinite(candidate_pitch)
    either_voiced = np.isfinite(reference_pitch) | np.isfinite(candidate_pitch)
    cents = 1200 * np.abs(
        np.log2(reference_pitch[both_voiced] / candidate_pitch[both_voiced])
    )

    candidate_spectral_steps = np.sqrt(
        np.mean(np.diff(candidate_features[0], axis=1) ** 2, axis=0)
    )
    reference_spectral_steps = np.sqrt(
        np.mean(np.diff(reference_features[0], axis=1) ** 2, axis=0)
    )
    boundary_interval = stream_hop // FEATURE_HOP
    if boundary_interval <= 0:
        raise ValueError("stream hop must be at least one 10 ms feature frame")
    boundary_frames = np.arange(
        boundary_interval, candidate_spectral_steps.size, boundary_interval
    )
    sample_steps = np.abs(np.diff(candidate))
    boundary_samples = np.arange(stream_hop, sample_steps.size, stream_hop)

    return {
        "reference_duration_s": reference_duration,
        "candidate_duration_s": candidate_duration,
        "duration_delta_s": candidate_duration - reference_duration,
        "compared_duration_s": candidate.size / SAMPLE_RATE,
        "alignment_lag_ms": lag * 10,
        "logmel_corr": mel_correlation,
        "logmel_mae_db": float(np.mean(np.abs(reference_mel - candidate_mel))),
        "rms_envelope_corr": correlation(reference_rms, candidate_rms),
        "rms_ratio_db": float(
            20
            * np.log10(
                np.sqrt(np.mean(candidate**2) + 1e-12)
                / np.sqrt(np.mean(reference**2) + 1e-12)
            )
        ),
        "centroid_median_delta_hz": float(
            np.median(candidate_centroid) - np.median(reference_centroid)
        ),
        "f0_median_cents": float(np.median(cents)) if cents.size else None,
        "f0_p90_cents": float(np.percentile(cents, 90)) if cents.size else None,
        "voiced_iou": float(
            np.count_nonzero(both_voiced)
            / max(np.count_nonzero(either_voiced), 1)
        ),
        "clip_percent": float(np.mean(np.abs(candidate) >= 0.999) * 100),
        "boundary_spectral_step_mean_db": optional_mean(
            candidate_spectral_steps[boundary_frames - 1]
        ),
        "reference_boundary_spectral_step_mean_db": optional_mean(
            reference_spectral_steps[boundary_frames - 1]
        ),
        "all_spectral_step_mean_db": float(np.mean(candidate_spectral_steps)),
        "boundary_sample_jump_mean": optional_mean(
            sample_steps[boundary_samples - 1]
        ),
        "all_sample_jump_mean": float(np.mean(sample_steps)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    parser.add_argument("candidates", nargs="+")
    parser.add_argument("--stream-hop", type=int, required=True)
    args = parser.parse_args()
    for candidate in args.candidates:
        print(candidate)
        print(json.dumps(
            compare(args.reference, candidate, args.stream_hop), indent=2
        ))


if __name__ == "__main__":
    main()
