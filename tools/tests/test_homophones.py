"""同音纠错：命中、保护与空词表。"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import homophones

TERMS = {'网赌': 'online gambling', '网贷': 'online lending', '戒赌': 'quit gambling'}


class HomophoneTests(unittest.TestCase):
    def test_misspelled_terms_are_replaced(self):
        self.assertEqual(homophones.correct('碰到王独的人', TERMS),
                         ('碰到网赌的人', [{'from': '王独', 'to': '网赌'}]))
        self.assertEqual(homophones.correct('他借往代了', TERMS),
                         ('他借网贷了', [{'from': '往代', 'to': '网贷'}]))

    def test_correct_text_and_terms_stay_unchanged(self):
        for text in ('他和网赌没关系', '我们一起去公园散步', '这就是麻醉剂'):
            with self.subTest(text=text):
                self.assertEqual(homophones.correct(text, TERMS), (text, []))

    def test_common_words_are_protected_by_frequency(self):
        for text in ('我和姐姐一起去的', '我和哥哥一起去的', '妈妈说要回家'):
            with self.subTest(text=text):
                self.assertEqual(homophones.correct(text, {'格格': 'Gege', '戒戒': 'Jiejie'})[0], text)

    def test_rare_spans_are_still_corrected(self):
        self.assertEqual(homophones.correct('碰到王独的人', TERMS)[0], '碰到网赌的人')

    def test_explicit_corrections_win_over_the_stopword_list(self):
        self.assertEqual(homophones.correct('我和姐姐一起去的', {}, corrections={'姐姐': '戒戒'}),
                         ('我和戒戒一起去的', [{'from': '姐姐', 'to': '戒戒', 'count': 1}]))

    def test_corrections_file_is_validated(self):
        import json, tempfile, pathlib
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/'corrections.json'
            path.write_text(json.dumps({'姐姐': '戒戒'}), encoding='utf-8')
            self.assertEqual(homophones.load_corrections(path), {'姐姐': '戒戒'})
            path.write_text(json.dumps({'sister': '戒戒'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                homophones.load_corrections(path)

    def test_empty_terms_disable_correction_without_pinyin(self):
        self.assertEqual(homophones.correct('碰到王独的人', {}), ('碰到王独的人', []))

    def test_non_chinese_spans_are_ignored(self):
        text = '2026 年 wangdu 王独'
        self.assertEqual(homophones.correct(text, TERMS)[0], '2026 年 wangdu 网赌')


if __name__ == '__main__':
    unittest.main()
