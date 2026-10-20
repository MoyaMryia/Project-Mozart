import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import glossary


class GlossaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.directory.name)/'glossary.json'

    def tearDown(self):
        self.directory.cleanup()

    def test_defaults_apply_without_a_file(self):
        terms = glossary.load(self.path)
        self.assertEqual(terms['网赌'], 'online gambling')
        self.assertEqual(len(terms), len(glossary.DEFAULT_GLOSSARY))

    def test_file_adds_and_overrides_terms(self):
        self.path.write_text(json.dumps({'网赌': 'Gambling Online', '新词': 'new term'}), encoding='utf-8')
        terms = glossary.load(self.path)
        self.assertEqual(terms['网赌'], 'gambling online')
        self.assertEqual(terms['新词'], 'new term')
        self.assertEqual(terms['网贷'], 'online lending')

    def test_invalid_entries_fail_before_use(self):
        for bad in ({'gambling': 'online gambling'}, {'网赌': '在线赌博'}, {'网赌': ''}):
            with self.subTest(entry=bad):
                self.path.write_text(json.dumps(bad), encoding='utf-8')
                with self.assertRaises(ValueError):
                    glossary.load(self.path)

    def test_malformed_json_names_the_file(self):
        self.path.write_text('{', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '不是合法 JSON'):
            glossary.load(self.path)

    def test_save_round_trips_and_rejects_invalid_input(self):
        glossary.save({'网赌': 'Online Gambling'}, self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'网赌': 'online gambling'})
        with self.assertRaises(ValueError):
            glossary.save({'bad': 'term'}, self.path)

    def test_save_requires_a_target(self):
        with self.assertRaises(ValueError):
            glossary.save({'网赌': 'online gambling'})


if __name__ == '__main__':
    unittest.main()
