#!/usr/bin/env python3
"""使用固定录音比较流式 ASR。每个进程只加载一个模型。"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import sys
import time
import unicodedata
import wave

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
FEATURE_DIM = 80
PCM_BYTES = 2
TAIL_SECONDS = 2.0


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('线程数必须大于零')
    return number


def normalize(text):
    return ''.join(c for c in text.casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def edit_distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        current = [i]
        for j, right in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def load_samples(manifest):
    if not isinstance(manifest, dict):
        raise ValueError('输入清单必须为 JSON 对象')
    samples = manifest.get('samples')
    if not isinstance(samples, list) or not samples:
        raise ValueError('输入清单必须包含录音列表')
    seen = set()
    loaded = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError('录音条目必须为 JSON 对象')
        for field in ('id', 'cohort', 'path', 'sha256'):
            if not isinstance(sample.get(field), str) or not sample[field]:
                raise ValueError(f'录音字段必须为非空字符串：{field}')
        if 'reference' not in sample:
            raise ValueError('录音必须显式声明 reference，缺少人工转写时使用 null')
        reference = sample['reference']
        if reference is not None and (not isinstance(reference, str) or not normalize(reference)):
            raise ValueError('人工转写必须包含文字或数字')
        name = sample['id']
        if name in seen:
            raise ValueError(f'录音标识重复：{name}')
        seen.add(name)
        path = Path(sample['path'])
        if sha256(path) != sample['sha256']:
            raise ValueError(f'录音 SHA-256 不匹配：{path}')
        with wave.open(str(path)) as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, PCM_BYTES, SAMPLE_RATE):
                raise ValueError(f'录音必须使用单声道、PCM16、{SAMPLE_RATE} Hz：{path}')
            frames = source.getnframes()
            pcm = source.readframes(frames)
        if not frames or len(pcm) != frames * PCM_BYTES:
            raise ValueError(f'录音为空或 PCM 数据不完整：{path}')
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        loaded.append((sample, audio))
    return loaded


def decode(recognizer, audio, paced):
    stream = recognizer.create_stream()
    source_seconds = len(audio) / SAMPLE_RATE
    inputs = np.concatenate((audio, np.zeros(int(SAMPLE_RATE * TAIL_SECONDS), dtype=np.float32)))
    trace = []
    decode_steps = []
    active_seconds = 0.0
    max_feed_lateness_s = 0.0
    previous_text = ''
    first_text_input_s = None
    first_text_wall_s = None
    started = time.perf_counter()

    def drain():
        while recognizer.is_ready(stream):
            begin = time.perf_counter()
            recognizer.decode_stream(stream)
            decode_steps.append((time.perf_counter() - begin) * 1000)

    def observe(input_seconds):
        nonlocal previous_text, first_text_input_s, first_text_wall_s
        text = recognizer.get_result(stream)
        if text != previous_text:
            now = time.perf_counter() - started
            trace.append(dict(input_seconds=input_seconds, wall_seconds=now, text=text))
            previous_text = text
            if first_text_input_s is None and any(c.isalnum() for c in text):
                first_text_input_s = input_seconds
                first_text_wall_s = now
        return text

    for offset in range(0, len(inputs), FRAME_SAMPLES):
        end = min(offset + FRAME_SAMPLES, len(inputs))
        input_seconds = end / SAMPLE_RATE
        if paced:
            time.sleep(max(0, started + input_seconds - time.perf_counter()))
        begin = time.perf_counter()
        if paced:
            max_feed_lateness_s = max(max_feed_lateness_s, begin - started - input_seconds)
        stream.accept_waveform(SAMPLE_RATE, inputs[offset:end])
        drain()
        observe(input_seconds)
        active_seconds += time.perf_counter() - begin
    begin = time.perf_counter()
    stream.input_finished()
    drain()
    final_text = observe(len(inputs) / SAMPLE_RATE)
    active_seconds += time.perf_counter() - begin
    return dict(text=final_text, audio_seconds=source_seconds, active_seconds=active_seconds,
                wall_seconds=time.perf_counter() - started,
                rtf_with_tail=active_seconds / source_seconds,
                first_text_input_s=first_text_input_s, first_text_wall_s=first_text_wall_s,
                max_feed_lateness_s=max_feed_lateness_s if paced else None,
                trace=trace, decode_steps_ms=decode_steps)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--threads', type=positive_int, default=2)
    parser.add_argument('--paced', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    config = json.loads(args.model_config.read_text())
    if not isinstance(config, dict):
        raise ValueError('模型配置必须为 JSON 对象')
    samples = load_samples(manifest)
    for field in ('id', 'model_type', 'encoder', 'decoder', 'joiner', 'tokens'):
        if not isinstance(config.get(field), str):
            raise ValueError(f'模型字段必须为字符串：{field}')
    assets = {name: dict(path=config[name], sha256=sha256(config[name]), bytes=Path(config[name]).stat().st_size)
              for name in ('encoder', 'decoder', 'joiner', 'tokens')}

    import sherpa_onnx
    begin = time.perf_counter()
    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        **{key: config[key] for key in assets}, model_type=config['model_type'],
        provider='cpu', num_threads=args.threads, sample_rate=SAMPLE_RATE, feature_dim=FEATURE_DIM,
        decoding_method='greedy_search', enable_endpoint_detection=False,
    )
    initialization_seconds = time.perf_counter() - begin
    header = dict(model=config['id'], assets=assets, model_type=config['model_type'],
                  manifest=str(args.manifest), manifest_sha256=sha256(args.manifest),
                  model_config=str(args.model_config), model_config_sha256=sha256(args.model_config),
                  probe_sha256=sha256(__file__), python_version=sys.version, numpy_version=np.__version__,
                  sherpa_version=sherpa_onnx.__version__, threads=args.threads, provider='cpu',
                  frame_samples=FRAME_SAMPLES, feature_dim=FEATURE_DIM,
                  sample_rate=SAMPLE_RATE, tail_seconds=TAIL_SECONDS,
                  endpoint_detection=False, paced=args.paced, initialization_seconds=initialization_seconds)
    with args.output.open('x') as output:
        output.write(json.dumps(dict(type='config', **header), ensure_ascii=False) + '\n')
        output.flush()
        for sample, audio in samples:
            result = decode(recognizer, audio, args.paced)
            if sample['reference'] is not None:
                reference = normalize(sample['reference'])
                result.update(character_edits=edit_distance(reference, normalize(result['text'])),
                              reference_characters=len(reference))
            result.update(id=sample['id'], cohort=sample['cohort'],
                          sample_sha256=sample['sha256'], reference_present=sample['reference'] is not None,
                          process_peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
            output.write(json.dumps(dict(type='result', **result), ensure_ascii=False) + '\n')
            output.flush()
            print(json.dumps(dict(model=config['id'], id=sample['id'], text=result['text'],
                                  rtf_with_tail=round(result['rtf_with_tail'], 3)), ensure_ascii=False), flush=True)
        output.write(json.dumps(dict(type='complete', samples=len(samples)), ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
