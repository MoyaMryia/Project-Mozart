from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_chunks import split_speech_text, word_units


class SpeechChunkTests(unittest.TestCase):
    def test_repeated_clauses_keep_exact_phrase_count(self):
        text = 'After that, there should be a period of calm and a wave of unrest, a wave of unrest, a wave of unrest.'
        pieces = split_speech_text(text)
        self.assertEqual(pieces, ['After that, there should be a period of calm and a wave',
                                 'of unrest,', 'a wave of unrest,', 'a wave of unrest.'])
        self.assertEqual(' '.join(pieces), text)

    def test_short_repeated_words_do_not_force_fragments(self):
        self.assertEqual(split_speech_text('Yes, yes, I will explain.'), ['Yes, yes, I will explain.'])

    def test_expanded_number_splits_the_repeating_soak_sentence(self):
        text = 'After winning 25,000, I am afraid to tell them it is gambling.'
        pieces = split_speech_text(text)
        self.assertEqual(pieces, ['After winning 25,000,', 'I am afraid to tell them it is gambling.'])
        self.assertEqual(' '.join(pieces), text)

    def test_numeric_ranges_expand_both_endpoints(self):
        self.assertEqual(word_units('20,000–30,000,'),8)
        self.assertEqual(word_units('38,000+'),4)
        self.assertEqual(word_units('2020-2022'),8)
        self.assertEqual(word_units('iPhone14Pro'),1)

    def test_preserves_text_numbers_and_negation(self):
        text = 'Do not pay 70,000–80,000 dollars. Keep the 2022 statement unchanged, and explain why the original amount was incorrect.'
        pieces = split_speech_text(text)
        self.assertEqual(' '.join(pieces), text)
        self.assertTrue(all(len(p.split()) <= 15 for p in pieces))
        self.assertEqual(pieces[0], 'Do not pay 70,000–80,000 dollars.')

    def test_no_punctuation_or_tiny_final_piece(self):
        text = ' '.join(str(i) for i in range(26))
        pieces = split_speech_text(text)
        self.assertEqual(' '.join(pieces), text)
        self.assertEqual([len(p.split()) for p in pieces], [12,14])

    def test_chinese_preserves_characters(self):
        text = '这是用来验证分段后没有丢失任何汉字或者数字的句子，2022年的数字也必须原样保留。'
        pieces = split_speech_text(text, 'zh')
        self.assertEqual(''.join(pieces), text)
        self.assertTrue(all(len(p) <= 24 for p in pieces))

    def test_preposition_stays_with_its_phrase(self):
        text = "she paid a certain amount of gold coins to her mother to enroll in the master's degree. 700."
        pieces = split_speech_text(text)
        self.assertEqual(' '.join(pieces), text)
        self.assertEqual(pieces[0], 'she paid a certain amount of gold coins to her mother')
        self.assertTrue(all(not p.endswith(' to') for p in pieces))

    def test_nearby_sentence_end_keeps_noun_phrase_together(self):
        text="In the following six months, I acted with concrete actions to show them that this is crocodile tears. Oh, by the way, May 2022 hadn't even finished yet."
        pieces=split_speech_text(text)
        self.assertEqual(' '.join(pieces),text)
        self.assertTrue(any('crocodile tears.' in p for p in pieces))
        self.assertTrue(all(len(p.split())<=15 for p in pieces))

    def test_sentence_extension_preserves_existing_clause_boundary(self):
        text='This is called account provision; account provision means helping online gambling platforms provide accounts.'
        self.assertEqual(split_speech_text(text),['This is called account provision;', 'account provision means helping online gambling platforms provide accounts.'])

    def test_value_stays_with_its_inventory_clause(self):
        text="But actually, I don't owe anyone money because I have inventory worth over 38,000 that I won."
        pieces=split_speech_text(text)
        self.assertEqual(' '.join(pieces),text)
        self.assertTrue(any('inventory worth over 38,000' in p for p in pieces))
