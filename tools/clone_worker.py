#!/usr/bin/env python3
"""Isolated, warm sherpa-onnx reference TTS worker; JSON lines in and out."""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from onnxruntime_info import require_cuda, sherpa_runtime_info


def early_runtime(module):
    package = Path(module.__file__).parent
    manifest = json.loads((package/'mozart_early_decode.json').read_text())
    extension = next((package/'lib').glob('_sherpa_onnx*.so'))
    if manifest.get('protocol') != 1 or hashlib.sha256(extension.read_bytes()).hexdigest() != manifest['extension_sha256']:
        raise RuntimeError('Early audio runtime does not match its build manifest')
    return manifest


class AudioBlocks:
    """Write ordered PCM blocks before the final synthesis response."""
    def __init__(self, output, rate, limit, started, numpy, soundfile):
        self.output, self.rate, self.limit, self.started = Path(output), rate, limit, started
        self.np, self.sf = numpy, soundfile
        self.count = self.samples = self.limited = 0
        self.error = None
        self.previous = started

    def emit(self, samples):
        pcm = self.np.asarray(samples, dtype='float32')
        if not len(pcm):
            return True
        peak = float(self.np.max(self.np.abs(pcm)))
        if not self.np.isfinite(pcm).all() or peak > 4:
            self.error = 'Early audio contains nonfinite or unsafe-amplitude samples'
            return False
        if (self.samples+len(pcm))/self.rate > self.limit:
            self.error = 'Early audio exceeded the duration limit'
            return False
        limited = int(self.np.count_nonzero(self.np.abs(pcm) > .98))
        filename = self.output.stem+f'-block-{self.count:05d}.wav'
        path = self.output.parent/filename
        temporary = path.with_suffix('.tmp')
        try:
            self.sf.write(temporary, self.np.clip(pcm, -.98, .98), self.rate, subtype='PCM_16', format='WAV')
            temporary.replace(path)
        except Exception as error:
            self.error = f'Early audio storage failed: {error}'
            temporary.unlink(missing_ok=True)
            return False
        now = time.monotonic()
        print(json.dumps({'type': 'audio', 'block_index': self.count, 'filename': filename,
            'duration_seconds': len(pcm)/self.rate, 'sample_rate': self.rate,
            'synthesis_seconds': now-self.previous, 'elapsed_seconds': now-self.started,
            'raw_peak': peak, 'output_gain': 1.0, 'limited_samples': limited}), flush=True)
        self.count += 1
        self.samples += len(pcm)
        self.limited += limited
        self.previous = now
        return True


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
    if job.get('live') or job.get('chunk_count', 1) > 1 or job.get('early_audio'):
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
    parser.add_argument('--early-audio', action='store_true')
    args = parser.parse_args()
    if args.early_audio and args.engine != 'pocket':
        parser.error('--early-audio requires the pocket engine')
    if args.engine != 'pocket' and args.precision != 'int8':
        parser.error('--precision float32 requires the pocket engine')
    import numpy as np
    import soundfile as sf
    import sherpa_onnx as so
    early = early_runtime(so) if args.early_audio else None
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
                      'provider_requested': args.provider, 'precision': args.precision,
                      'audio_stream_protocol': 1 if early else 0,
                      'early_decode_runtime': early, **runtime}), flush=True)
    for line in sys.stdin:
        job = json.loads(line)
        start = time.monotonic()
        first = None
        received = 0
        limit = job['max_duration_seconds']
        blocks = AudioBlocks(job['output'], tts.sample_rate, limit, start, np, sf) if early and job.get('early_audio') else None
        def callback(samples, progress):
            nonlocal first, received
            if first is None:
                first = time.monotonic() - start
            received += len(samples)
            if blocks:
                return int(blocks.emit(samples))
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
                if blocks:
                    config.extra = {**config.extra, 'early_decode': '1'}
            synthesis_text = pocket_input_text(job['text']) if args.engine == 'pocket' else job['text']
            audio = tts.generate(synthesis_text, config, callback)
            if blocks and blocks.error:
                raise ValueError(blocks.error)
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
            if blocks:
                if blocks.samples != len(pcm):
                    raise ValueError('Early audio sample count differs from the final output')
                pcm = np.clip(pcm, -.98, .98)
                gain = 1.0
            else:
                pcm *= gain
            sf.write(job['output'], pcm, audio.sample_rate, subtype='PCM_16', format='WAV')
            print(json.dumps({'type': 'complete', 'duration_seconds': duration, 'sample_rate': audio.sample_rate,
                'synthesis_seconds': time.monotonic()-start, 'first_callback_seconds': first,
                'synthesis_text': synthesis_text,
                'raw_peak': peak, 'output_gain': gain,
                'stream_blocks': blocks.count if blocks else 0,
                'limited_samples': blocks.limited if blocks else 0}), flush=True)
        except Exception as error:
            print(json.dumps({'error': str(error)}), flush=True)


if __name__ == '__main__':
    main()
