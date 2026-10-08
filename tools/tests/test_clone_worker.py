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
