"""离线语义契约的回归测试。固定样本，全部在本地读取 fixture 和已记录结果。"""
import hashlib
import json
import pathlib
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import translation_semantics as semantics

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESULTS = ROOT/'rvc-golden/asr-selection-20261028/results.json'
RECORDED_PASSING = (8, 16, 17, 39, 41, 42, 43)
INCOMPLETE_RECORD = 9


def recorded_english():
    rows = json.loads(RESULTS.read_text(encoding='utf-8'))['records']
    return {row['seq']: row['candidate'].get('en', '') for row in rows}


class FixtureContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = semantics.load_contract()

    def test_contract_fingerprint_matches_the_validated_input(self):
        self.assertEqual(self.contract.source_sha256,
                         hashlib.sha256(semantics.FIXTURE_PATH.read_bytes()).hexdigest())

    def test_record_16_uses_the_selected_source_not_the_original_asr(self):
        rows = json.loads(RESULTS.read_text())['records']
        source = next(row['candidate']['source'] for row in rows if row['seq'] == 16)
        self.assertEqual(self.contract.by_seq[16].source_zh, source.rstrip('。'))

    def test_every_case_annotates_subject_relation_object_quantity_and_negation(self):
        for case in self.contract.cases:
            with self.subTest(case=case.id):
                self.assertEqual(set(case.annotations), set(semantics.ANNOTATION_KEYS))
                for value in case.annotations.values():
                    self.assertTrue(value.strip())

    def test_recorded_cases_and_reserved_samples_keep_their_sequences(self):
        self.assertEqual({case.seq for case in self.contract.cases if case.seq is not None},
                         set(RECORDED_PASSING) | {INCOMPLETE_RECORD})
        reserved = [case for case in self.contract.cases if case.seq is None]
        self.assertGreaterEqual(len(reserved), 5)

    def test_reference_and_alias_translations_pass(self):
        for case in self.contract.cases:
            with self.subTest(case=case.id, english=case.reference_en):
                self.assertEqual(semantics.failures(case, case.reference_en), [])
            for english in case.accept:
                with self.subTest(case=case.id, english=english):
                    self.assertEqual(semantics.failures(case, english), [])

    def test_counterexamples_fail_the_named_facts(self):
        for case in self.contract.cases:
            for item in case.reject:
                with self.subTest(case=case.id, english=item['en']):
                    self.assertEqual(semantics.failures(case, item['en']), list(item['fails']))

    def test_complete_references_pass_without_terminal_punctuation(self):
        # 完整样本的结尾由 facts 断言，不依赖句末标点。
        for case in self.contract.cases:
            if case.completeness != 'complete':
                continue
            english = case.reference_en.rstrip('.!?')
            with self.subTest(case=case.id, english=english):
                self.assertEqual(semantics.failures(case, english), [])

    def test_fragment_completions_fail_with_and_without_punctuation(self):
        record_9 = self.contract.by_seq[9]
        for english in ('We shall do what we must do.', 'We shall do what we must do'):
            with self.subTest(english=english):
                self.assertIn('open_copula_ending', semantics.failures(record_9, english))
        open_case = next(case for case in self.contract.cases if case.id == 'reserved_open_condition')
        for english in ('If the weather is good tomorrow, we will go out.',
                        'If the weather is good tomorrow, we will go out'):
            with self.subTest(english=english):
                self.assertIn('open_condition_ending', semantics.failures(open_case, english))


class RecordedResultsSemanticsTests(unittest.TestCase):
    def test_only_the_incomplete_record_fails_the_contract(self):
        contract, english = semantics.load_contract(), recorded_english()
        checks = contract.checks(english)
        for seq in RECORDED_PASSING:
            with self.subTest(seq=seq):
                self.assertTrue(checks['semantic_record_' + str(seq)])
        self.assertFalse(checks['semantic_record_' + str(INCOMPLETE_RECORD)])
        case = contract.by_seq[INCOMPLETE_RECORD]
        self.assertEqual(english[INCOMPLETE_RECORD], 'We shall do what we must do.')
        self.assertEqual(semantics.failures(case, english[INCOMPLETE_RECORD]), ['what_fronted', 'open_copula_ending'])

    def test_missing_candidate_text_counts_as_failure(self):
        checks = semantics.load_contract().checks({})
        self.assertTrue(checks)
        self.assertFalse(any(checks.values()))


class FixtureValidationTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(semantics.FIXTURE_PATH.read_text(encoding='utf-8'))

    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/'contract.json'
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            return semantics.load_contract(path)

    def test_bad_version_fails(self):
        self.data['version'] = 2
        with self.assertRaisesRegex(semantics.ContractError, 'version'):
            self.load(self.data)

    def test_bad_regex_fails(self):
        self.data['facts']['genshin_is_game']['patterns'] = ['(unclosed']
        with self.assertRaisesRegex(semantics.ContractError, '正则无效'):
            self.load(self.data)

    def test_unknown_reject_fact_fails(self):
        self.data['cases'][0]['reject'][0]['fails'] = ['not_a_fact']
        with self.assertRaisesRegex(semantics.ContractError, '未知断言'):
            self.load(self.data)

    def test_missing_annotation_fails(self):
        del self.data['cases'][0]['annotations']['negation']
        with self.assertRaisesRegex(semantics.ContractError, 'annotations'):
            self.load(self.data)


class RecordedResultsCommandTests(unittest.TestCase):
    def run_cli(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/'results.json'
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            return subprocess.run([sys.executable, str(ROOT/'rvc-golden/asr-selection-20261028/probe.py'),
                '--recorded-results', str(path), '--llama-server', '/missing-model-server'],
                capture_output=True, text=True, timeout=10)

    def test_historical_failure_returns_nonzero_without_a_model(self):
        result = self.run_cli(json.loads(RESULTS.read_text()))
        self.assertEqual(result.returncode, 1)
        checks = json.loads(result.stdout)
        self.assertTrue(checks['all_candidates_completed'])
        self.assertFalse(checks['semantic_record_9'])
        self.assertEqual(result.stderr, '')

    def test_references_pass_and_changed_reserved_quantity_fails(self):
        data = json.loads(RESULTS.read_text())
        contract = semantics.load_contract()
        for row in data['records']:
            if row['seq'] in contract.by_seq:
                row['candidate']['en'] = contract.by_seq[row['seq']].reference_en
        data['reserved_results'] = {case.id: {'status': 'completed', 'en': case.reference_en}
                                    for case in contract.cases if case.seq is None}
        self.assertEqual(self.run_cli(data).returncode, 0)
        data['reserved_results']['reserved_printer_quantity']['en'] = 'We bought four printers.'
        result = self.run_cli(data)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['semantic_reserved_printer_quantity'])

    def test_missing_reserved_model_outputs_prevent_full_acceptance(self):
        data = json.loads(RESULTS.read_text())
        contract = semantics.load_contract()
        for row in data['records']:
            if row['seq'] in contract.by_seq:
                row['candidate']['en'] = contract.by_seq[row['seq']].reference_en
        for reserved in (None, {}):
            with self.subTest(reserved=reserved):
                data['reserved_results'] = reserved
                result = self.run_cli(data)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(json.loads(result.stdout)['semantic_reserved_printer_quantity'])

    def test_invalid_recorded_input_fails_at_the_entry(self):
        result = self.run_cli({'records': []})
        self.assertEqual(result.returncode, 2)
        self.assertIn('nonempty records array', result.stderr)


if __name__ == '__main__':
    unittest.main()
