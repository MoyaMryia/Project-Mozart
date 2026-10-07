import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clone_worker import configure_pocket, pocket_input_text
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
