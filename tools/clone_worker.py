#!/usr/bin/env python3
"""Isolated, warm sherpa-onnx reference TTS worker; JSON lines in and out."""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from onnxruntime_info import require_cuda, sherpa_runtime_info


def pocket_input_text(text):
    # Unpunctuated fragments and ellipses repeatedly hit the generation cap
    # in fixed-reference controls. Mark the ending without changing any words.
    text = re.sub(r'\.{2,}|…+', '.', text.strip())
    text = re.sub(r'[,;:]+(?=[\"\'”’)]*$)', '.', text)
    if not re.search(r'[.!?][\"\'”’)]*$', text):
        text += '.'
    return text


def configure_pocket(config, job, limit):
    maximum_frames = None
    extra = {'seed': '42'}
    if job.get('live') or job.get('chunk_count', 1) > 1:
        maximum_frames = max(1, int(limit*12.5))
        extra['max_frames'] = str(maximum_frames)
    # The pybind map getter returns a copy. Mutating config.extra in place
    # silently loses options; assign the complete map through its setter.
    config.extra = extra
    return maximum_frames


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', choices=['pocket', 'zipvoice'], required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--vocoder', default='')
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--provider', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--precision', choices=['int8', 'float32'], default='int8')
    parser.add_argument('--provider-config', type=Path)
    args = parser.parse_args()
    if args.engine != 'pocket' and args.precision != 'int8':
        parser.error('--precision float32 requires the pocket engine')
    import numpy as np
    import soundfile as sf
    import sherpa_onnx as so
    try:
        runtime = sherpa_runtime_info(so)
    except (OSError, RuntimeError) as error:
        if args.provider == 'cuda':
            raise
        runtime = {'runtime_probe_error': str(error)}
    if args.provider == 'cuda':
        require_cuda(runtime)
    provider = args.provider
    if args.provider_config:
        provider += ':'+str(args.provider_config.resolve())
    m = args.model
    if args.engine == 'pocket':
        suffix = '.int8.onnx' if args.precision == 'int8' else '.onnx'
        model = so.OfflineTtsPocketModelConfig(**{
            key: str(m / filename) for key, filename in {
                'lm_flow': 'lm_flow'+suffix, 'lm_main': 'lm_main'+suffix,
                'encoder': 'encoder.onnx', 'decoder': 'decoder'+suffix,
                'text_conditioner': 'text_conditioner.onnx', 'vocab_json': 'vocab.json',
                'token_scores_json': 'token_scores.json'}.items()})
    else:
        model = so.OfflineTtsZipvoiceModelConfig(
            encoder=str(m/'encoder.int8.onnx'), decoder=str(m/'decoder.int8.onnx'),
            tokens=str(m/'tokens.txt'), lexicon=str(m/'lexicon.txt'),
            data_dir=str(m/'espeak-ng-data'), vocoder=args.vocoder)
    tts = so.OfflineTts(so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(
        **{args.engine: model}, num_threads=args.threads, provider=provider)))
    print(json.dumps({'ready': True, 'runtime': so.__version__, 'engine': args.engine,
                      'provider_requested': args.provider, 'precision': args.precision, **runtime}), flush=True)
    for line in sys.stdin:
        job = json.loads(line)
        start = time.monotonic()
        first = None
        received = 0
        limit = job['max_duration_seconds']
        def callback(samples, progress):
            nonlocal first, received
            if first is None:
                first = time.monotonic() - start
            received += len(samples)
            return int(received / tts.sample_rate <= limit)
        try:
            reference, rate = sf.read(job['reference'], dtype='float32')
            config = so.GenerationConfig()
            config.reference_audio = reference
            config.reference_sample_rate = rate
            config.reference_text = job.get('reference_text', '')
            config.num_steps = 5 if args.engine == 'pocket' else 4
            config.speed = 1
            config.silence_scale = 1
            maximum_frames = None
            if args.engine == 'pocket':
                # Pocket callbacks run during decoding, after its latent loop.
                maximum_frames = configure_pocket(config, job, limit)
            synthesis_text = pocket_input_text(job['text']) if args.engine == 'pocket' else job['text']
            audio = tts.generate(synthesis_text, config, callback)
            pcm = np.asarray(audio.samples, dtype='float32')
            duration = len(pcm) / audio.sample_rate
            if not len(pcm) or not np.isfinite(pcm).all():
                raise ValueError('Engine returned empty or non-finite audio')
            if duration > limit or received / tts.sample_rate > limit:
                raise ValueError('Generated speech exceeded the duration limit; result rejected')
            if maximum_frames is not None and duration >= maximum_frames*.08-.001:
                raise ValueError('Generation reached the frame limit without a verified ending; result rejected')
            peak = float(np.max(np.abs(pcm)))
            if peak > 4:
                raise ValueError('Engine amplitude exceeds the safe range; result rejected')
            gain = min(1.0, .98/peak) if peak else 1.0
            pcm *= gain
            sf.write(job['output'], pcm, audio.sample_rate, subtype='PCM_16', format='WAV')
            print(json.dumps({'duration_seconds': duration, 'sample_rate': audio.sample_rate,
                'synthesis_seconds': time.monotonic()-start, 'first_callback_seconds': first,
                'synthesis_text': synthesis_text,
                'raw_peak': peak, 'output_gain': gain}), flush=True)
        except Exception as error:
            print(json.dumps({'error': str(error)}), flush=True)


if __name__ == '__main__':
    main()
