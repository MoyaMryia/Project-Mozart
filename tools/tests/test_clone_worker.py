import sys
from pathlib import Path
import unittest
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clone_worker import configure_pocket, pocket_input_text
import clone_worker
import re
import tempfile
import hashlib


class NativeOptions:
    """Match the installed pybind map's copy-on-read contract."""
    def __init__(self):
        self.value = {}

    @property
    def extra(self):
        return dict(self.value)

    @extra.setter
    def extra(self, value):
        self.value = dict(value)


class WorkerOptionsTests(unittest.TestCase):
    def test_early_runtime_manifest_rejects_a_changed_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package/'lib').mkdir()
            extension = package/'lib/_sherpa_onnx-test.so'
            extension.write_bytes(b'first build')
            manifest = {'protocol': 1, 'extension_sha256': hashlib.sha256(extension.read_bytes()).hexdigest()}
            (package/'mozart_early_decode.json').write_text(json.dumps(manifest))
            module = SimpleNamespace(__file__=str(package/'__init__.py'))
            self.assertEqual(clone_worker.early_runtime(module), manifest)
            extension.write_bytes(b'other build')
            with self.assertRaisesRegex(RuntimeError, 'build manifest'):
                clone_worker.early_runtime(module)

    def test_early_blocks_keep_order_and_limit_samples_before_publication(self):
        import numpy as np
        writes = []
        def write(path, pcm, *args, **kwargs):
            writes.append(pcm.copy())
            path.write_bytes(b'encoded PCM')
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, 'stdout', io.StringIO()) as output:
            blocks = clone_worker.AudioBlocks(Path(directory)/'part.tmp', 24000, 1,
                __import__('time').monotonic(), np, SimpleNamespace(write=write))
            self.assertTrue(blocks.emit([.1, -.2]))
            self.assertTrue(blocks.emit([1.5, -1.5]))
            self.assertFalse(blocks.emit([float('nan')]))
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([row['block_index'] for row in events], [0, 1])
            self.assertEqual(blocks.limited, 2)
            np.testing.assert_allclose(writes[0], [.1, -.2])
            np.testing.assert_allclose(writes[1], [.98, -.98])
            self.assertIn('nonfinite', blocks.error)

    def test_early_duration_limit_keeps_an_explicit_error(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, 'stdout', io.StringIO()) as output:
            blocks = clone_worker.AudioBlocks(Path(directory)/'part.tmp', 24000, .01,
                0, np, SimpleNamespace(write=Mock()))
            self.assertFalse(blocks.emit(np.zeros(241)))
            self.assertIn('duration limit', blocks.error)
            self.assertEqual(output.getvalue(), '')

    def native_stub(self):
        return SimpleNamespace(__version__='test', OfflineTts=Mock(), OfflineTtsConfig=Mock(),
                               OfflineTtsModelConfig=Mock(), OfflineTtsPocketModelConfig=Mock())

    def test_cuda_request_does_not_initialize_a_cpu_only_runtime(self):
        native = self.native_stub()
        args = ['clone_worker.py', '--engine', 'pocket', '--model', '/unused', '--provider', 'cuda']
        with patch.dict(sys.modules, numpy=Mock(), soundfile=Mock(), sherpa_onnx=native), \
                patch.object(sys, 'argv', args), \
                patch('clone_worker.sherpa_runtime_info', return_value={'available_providers':['CPUExecutionProvider']}), \
                patch.object(sys, 'stdout', io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'CPU fallback is disabled'):
                clone_worker.main()
        native.OfflineTts.assert_not_called()
        self.assertEqual(output.getvalue(), '')

    def test_cuda_request_reaches_native_config_with_float_assets(self):
        native = self.native_stub()
        args = ['clone_worker.py', '--engine', 'pocket', '--model', '/model',
                '--provider', 'cuda', '--precision', 'float32']
        with patch.dict(sys.modules, numpy=Mock(), soundfile=Mock(), sherpa_onnx=native), \
                patch.object(sys, 'argv', args), patch.object(sys, 'stdin', io.StringIO()), \
                patch('clone_worker.sherpa_runtime_info', return_value={'available_providers':['CUDAExecutionProvider','CPUExecutionProvider']}), \
                patch.object(sys, 'stdout', io.StringIO()) as output:
            clone_worker.main()
        self.assertEqual(native.OfflineTtsModelConfig.call_args.kwargs['provider'], 'cuda')
        self.assertEqual(native.OfflineTtsPocketModelConfig.call_args.kwargs['lm_main'], '/model/lm_main.onnx')
        ready = json.loads(output.getvalue())
        self.assertEqual(ready['provider_requested'], 'cuda')
        self.assertIn('CUDAExecutionProvider', ready['available_providers'])

    def test_cpu_start_survives_an_unavailable_runtime_probe(self):
        native = self.native_stub()
        args = ['clone_worker.py', '--engine', 'pocket', '--model', '/model']
        with patch.dict(sys.modules, numpy=Mock(), soundfile=Mock(), sherpa_onnx=native), \
                patch.object(sys, 'argv', args), patch.object(sys, 'stdin', io.StringIO()), \
                patch('clone_worker.sherpa_runtime_info', side_effect=OSError('No shared library')), \
                patch.object(sys, 'stdout', io.StringIO()) as output:
            clone_worker.main()
        self.assertEqual(native.OfflineTtsModelConfig.call_args.kwargs['provider'], 'cpu')
        ready = json.loads(output.getvalue())
        self.assertTrue(ready['ready'])
        self.assertEqual(ready['runtime_probe_error'], 'No shared library')

    def test_live_cap_reaches_native_options(self):
        config = NativeOptions()
        self.assertEqual(configure_pocket(config, {'live': True}, 4), 50)
        self.assertEqual(config.extra, {'seed': '42', 'max_frames': '50'})

    def test_multi_piece_cap_without_live_flag(self):
        config = NativeOptions()
        self.assertEqual(configure_pocket(config, {'chunk_count': 2}, 11), 137)
        self.assertEqual(config.extra['max_frames'], '137')

    def test_download_only_retains_engine_default_limit(self):
        config = NativeOptions()
        self.assertIsNone(configure_pocket(config, {}, 30))
        self.assertEqual(config.extra, {'seed': '42'})

    def test_failed_fragments_get_a_terminal_marker(self):
        for original, expected in [
            ('yes', 'yes.'),
            ("They didn't know that I had taken out a loan", "They didn't know that I had taken out a loan."),
            ('to confess. One is kidnapping someone, the other is...',
             'to confess. One is kidnapping someone, the other is.')]:
            self.assertEqual(pocket_input_text(original), expected)

    def test_existing_sentence_endings_remain(self):
        for text in ['Already done.', 'Really?', 'Stop!', 'He said "yes."']:
            self.assertEqual(pocket_input_text(text), text)

    def test_clause_ending_replaces_comma_instead_of_appending(self):
        self.assertEqual(pocket_input_text('After winning 25,000,'), 'After winning 25,000.')
        self.assertEqual(pocket_input_text('for self-improvement,'), 'for self-improvement.')
        self.assertEqual(pocket_input_text('He said "yes,"'), 'He said "yes."')

    def test_punctuation_preserves_words_numbers_and_order(self):
        for text in ['yes', "Didn't know... 12,000, then 5…", 'A loan from my family before.']:
            tokens = lambda s: re.findall(r"[\w]+(?:'[\w]+)?", s)
            self.assertEqual(tokens(text), tokens(pocket_input_text(text)))


if __name__ == '__main__':
    unittest.main()
