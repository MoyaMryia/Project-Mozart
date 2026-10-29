"""检查录音来源校验、流式输入和字符错误率计算。"""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

import numpy as np

SPEC = importlib.util.spec_from_file_location('xasr_probe', Path(__file__).with_name('probe.py'))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class Stream:
    def __init__(self):
        self.received = []
        self.pending = False
        self.finished = False
        self.flushed = False
        self.decoded = False

    def accept_waveform(self, rate, audio):
        assert rate == probe.SAMPLE_RATE
        self.received.extend(audio)
        self.pending = True

    def input_finished(self):
        self.finished = True


class Recognizer:
    def create_stream(self):
        self.stream = Stream()
        return self.stream

    def is_ready(self, stream):
        return stream.pending or (stream.finished and not stream.flushed)

    def decode_stream(self, stream):
        stream.pending = False
        stream.decoded = True
        if stream.finished:
            stream.flushed = True

    def get_result(self, stream):
        return '完成。' if stream.flushed else '片' if stream.decoded else ''


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'input.wav'

    def sample(self, channels=1, frames=320, rate=probe.SAMPLE_RATE):
        with wave.open(str(self.path), 'wb') as output:
            output.setnchannels(channels)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(b'\0\0' * frames * channels)
        return dict(id='sample', cohort='test', reference=None,
                    path=str(self.path), sha256=probe.sha256(self.path))

    def test_valid_input(self):
        sample = self.sample()
        loaded = probe.load_samples(dict(samples=[sample]))
        self.assertEqual(loaded[0][0], sample)
        self.assertEqual(len(loaded[0][1]), 320)
        self.assertEqual(loaded[0][1].dtype, np.float32)

    def test_pcm_normalization(self):
        sample = self.sample(frames=3)
        with wave.open(str(self.path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(probe.SAMPLE_RATE)
            output.writeframes(np.array([-32768, 0, 32767], dtype='<i2').tobytes())
        sample['sha256'] = probe.sha256(self.path)
        audio = probe.load_samples(dict(samples=[sample]))[0][1]
        np.testing.assert_array_equal(audio, [-1.0, 0.0, 32767 / 32768])

    def test_reference_must_be_explicit(self):
        sample = self.sample()
        del sample['reference']
        with self.assertRaisesRegex(ValueError, 'reference'):
            probe.load_samples(dict(samples=[sample]))

    def test_empty_reference_fails(self):
        sample = self.sample()
        sample['reference'] = '。 '
        with self.assertRaisesRegex(ValueError, '人工转写'):
            probe.load_samples(dict(samples=[sample]))

    def test_changed_audio_fails_hash_guard(self):
        sample = self.sample()
        self.path.write_bytes(self.path.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            probe.load_samples(dict(samples=[sample]))

    def test_stereo_fails_format_guard(self):
        with self.assertRaisesRegex(ValueError, '单声道'):
            probe.load_samples(dict(samples=[self.sample(channels=2)]))

    def test_wrong_rate_fails_format_guard(self):
        with self.assertRaisesRegex(ValueError, '16000'):
            probe.load_samples(dict(samples=[self.sample(rate=48000)]))

    def test_empty_audio_fails(self):
        with self.assertRaisesRegex(ValueError, '为空'):
            probe.load_samples(dict(samples=[self.sample(frames=0)]))

    def test_truncated_audio_fails_with_matching_hash(self):
        sample = self.sample()
        self.path.write_bytes(self.path.read_bytes()[:-2])
        sample['sha256'] = probe.sha256(self.path)
        with self.assertRaisesRegex(ValueError, '不完整'):
            probe.load_samples(dict(samples=[sample]))

    def test_duplicate_id_fails(self):
        sample = self.sample()
        with self.assertRaisesRegex(ValueError, '重复'):
            probe.load_samples(dict(samples=[sample, sample]))

    def test_empty_manifest_fails(self):
        with self.assertRaisesRegex(ValueError, '必须包含'):
            probe.load_samples(dict(samples=[]))

    def test_thread_argument(self):
        self.assertEqual(probe.positive_int('2'), 2)
        with self.assertRaises(argparse.ArgumentTypeError):
            probe.positive_int('0')

    def test_character_metric(self):
        self.assertEqual(probe.normalize('你，好！ A 681。'), '你好a681')
        self.assertNotEqual(probe.normalize('六百八十一'), probe.normalize('681'))
        self.assertEqual(probe.edit_distance('你好', '你号'), 1)
        self.assertEqual(probe.edit_distance('你好', '你'), 1)
        self.assertEqual(probe.edit_distance('你', '你好'), 1)
        self.assertEqual(probe.edit_distance('', '你'), 1)

    def test_frame_order_partial_frame_and_final_drain(self):
        recognizer = Recognizer()
        audio = np.arange(641, dtype=np.float32) / 1000
        result = probe.decode(recognizer, audio, paced=False)
        stream = recognizer.stream
        self.assertTrue(stream.finished)
        self.assertTrue(stream.flushed)
        self.assertEqual(result['text'], '完成。')
        np.testing.assert_array_equal(stream.received[:len(audio)], audio)
        np.testing.assert_array_equal(stream.received[len(audio):], np.zeros(int(probe.SAMPLE_RATE * probe.TAIL_SECONDS)))
        self.assertEqual(result['audio_seconds'], len(audio) / probe.SAMPLE_RATE)
        self.assertEqual(result['first_text_input_s'], probe.FRAME_SAMPLES / probe.SAMPLE_RATE)
        self.assertEqual(result['trace'][-1]['input_seconds'], result['audio_seconds'] + probe.TAIL_SECONDS)
        self.assertEqual(probe.TAIL_SECONDS, 2.0)
        self.assertAlmostEqual(result['rtf_with_tail'], result['active_seconds'] / result['audio_seconds'])
        self.assertLessEqual(result['active_seconds'], result['wall_seconds'])

    def test_main_records_provenance_reference_state_and_completion(self):
        sample = self.sample()
        referenced = dict(sample, id='reference', reference='完成')
        root = Path(self.directory.name)
        manifest_path = root / 'manifest.json'
        different = dict(sample, id='different', reference='完工')
        manifest_path.write_text(json.dumps(dict(samples=[sample, referenced, different])))
        asset = root / 'asset'
        asset.write_bytes(b'unit-test asset')
        config = dict(id='fake', model_type='zipformer2',
                      **{k: str(asset) for k in ('encoder', 'decoder', 'joiner', 'tokens')})
        config_path = root / 'model.json'
        config_path.write_text(json.dumps(config))
        output = root / 'result.jsonl'
        arguments = ['probe.py', '--manifest', str(manifest_path), '--model-config', str(config_path),
                     '--output', str(output)]
        factory = unittest.mock.Mock(return_value=Recognizer())
        runtime = types.SimpleNamespace(__version__='test', OnlineRecognizer=types.SimpleNamespace(from_transducer=factory))
        with patch.object(sys, 'argv', arguments), patch.dict(sys.modules, sherpa_onnx=runtime), contextlib.redirect_stdout(io.StringIO()):
            probe.main()
        records = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual([r['type'] for r in records], ['config', 'result', 'result', 'result', 'complete'])
        self.assertEqual(records[0]['feature_dim'], factory.call_args.kwargs['feature_dim'])
        self.assertEqual(records[0]['probe_sha256'], probe.sha256(Path(probe.__file__)))
        self.assertFalse(records[1]['reference_present'])
        self.assertNotIn('character_edits', records[1])
        self.assertTrue(records[2]['reference_present'])
        self.assertEqual(records[2]['character_edits'], 0)
        self.assertEqual(records[2]['reference_characters'], 2)
        self.assertEqual(records[2]['sample_sha256'], sample['sha256'])
        self.assertEqual(records[3]['character_edits'], 1)
        self.assertEqual(records[4]['samples'], 3)
        with patch.object(sys, 'argv', arguments), patch.dict(sys.modules, sherpa_onnx=runtime):
            with self.assertRaises(FileExistsError):
                probe.main()

    def test_rtf_counts_work_and_excludes_tail_from_denominator(self):
        clock = [0.0]

        class TimedStream(Stream):
            def accept_waveform(self, rate, audio):
                super().accept_waveform(rate, audio)
                clock[0] += 0.001

        class TimedRecognizer(Recognizer):
            def create_stream(self):
                self.stream = TimedStream()
                return self.stream

            def decode_stream(self, stream):
                super().decode_stream(stream)
                clock[0] += 0.010

            def get_result(self, stream):
                clock[0] += 0.001
                return super().get_result(stream)

        with patch.object(probe.time, 'perf_counter', side_effect=lambda: clock[0]):
            result = probe.decode(TimedRecognizer(), np.ones(640, dtype=np.float32), paced=False)
        self.assertAlmostEqual(result['active_seconds'], 1.235)
        self.assertAlmostEqual(result['rtf_with_tail'], 1.235 / 0.04)
        self.assertAlmostEqual(result['wall_seconds'], 1.235)


if __name__ == '__main__':
    unittest.main()
