import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subtitle_bridge import CaptionDispatcher, translation_source, caption_updates, source_caption, translation_glossary


FIXTURE = Path(__file__).parent / 'fixtures/asr-selection-20261028.json'


class SourceSelectionTests(unittest.TestCase):
    def test_recorded_short_correction_and_protected_names(self):
        records = {r['seq']: r for r in json.loads(FIXTURE.read_text())['records']}
        for seq, expected, reason in [
                (8, '原神是一款游戏', 'protected_term_changed'),
                (16, '中英文。', 'refined'),
                (42, '非常遗憾的是英国拒绝了我们的条款', 'protected_term_changed')]:
            with self.subTest(seq=seq):
                record, audit = records[seq], {}
                self.assertEqual(translation_source(record['zh'], record['refined_zh'], audit=audit), expected)
                self.assertEqual(audit['reason'], reason)
        self.assertEqual(records[8]['translation_source_text'], '元神是一款游戏。')
        self.assertEqual(records[16]['translation_source_text'], '中东英文')
        self.assertIn('已经国', records[42]['translation_source_text'])

    def test_short_correction_still_rejects_unrelated_text(self):
        for raw, refined in [('一起学习', '学习大。'), ('明', '嗯个。'), ('你好', '好。')]:
            with self.subTest(raw=raw):
                audit = {}
                self.assertEqual(translation_source(raw, refined, audit=audit), raw)
                self.assertEqual(audit['reason'], 'dissimilar_refinement')

    def test_configured_term_occurrence_counts_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'terms.json'
            path.write_text(json.dumps({'项目甲': 'Project Alpha'}))
            with patch.dict(os.environ, {'MOZART_GLOSSARY': str(path)}):
                for raw, refined in [
                        ('我们采用项目甲的方案', '我们采用项目乙的方案。'),
                        ('项目甲和项目甲都完成了', '项目甲都完成了。')]:
                    with self.subTest(raw=raw):
                        audit = {}
                        self.assertEqual(translation_source(raw, refined, audit=audit), raw)
                        self.assertEqual(audit['changed_terms'], ['项目甲'])

    def test_short_similarity_boundary_accepts_one_deletion(self):
        audit = {}
        self.assertEqual(translation_source('你好啊', '你好。', audit=audit), '你好。')
        self.assertEqual(audit['similarity'], .8)
        self.assertEqual(audit['selected'], 'refined')

    def test_small_spoken_number_formatting_does_not_block_translation(self):
        for raw, refined in [('我们三点见面吧', '我们3点见面吧。'),
                             ('十一点开饭', '11点开饭。'), ('两个小时后', '2个小时后。'),
                             ('下午两点半', '下午2点半。'), ('他一米八', '他1米8。')]:
            with self.subTest(raw=raw):
                audit = {}
                translation_source(raw, refined, audit=audit)
                self.assertEqual(audit['issues'], [])
                self.assertNotEqual(audit['reason'], 'quantity_conflict')

    def test_unrelated_refinement_does_not_create_quantity_or_polarity_conflicts(self):
        for raw, refined in [('我们一起去了那家新开的餐厅', '完全不同的识别结果可能800元'),
                             ('我们认为这可能成功', '完全无关的不可能的另一句内容')]:
            with self.subTest(raw=raw):
                audit = {}
                self.assertEqual(translation_source(raw, refined, audit=audit), raw)
                self.assertEqual(audit['reason'], 'dissimilar_refinement')
                self.assertEqual(audit['issues'], [])

    def test_quantity_conflict_is_reported_and_normalization_is_allowed(self):
        audit = {}
        self.assertEqual(translation_source('我们收到了七千元', '我们收到了700元。', audit=audit),
                         '我们收到了七千元')
        self.assertEqual(audit['issues'], ['refinement_quantity_disagreement'])
        self.assertEqual(translation_source('他是二零二二年来的', '他是2022年来的。'), '他是2022年来的。')

    def test_polarity_conflict_is_reported(self):
        audit = {}
        self.assertEqual(translation_source('我们认为这可能成功', '我们认为这不可能成功。', audit=audit),
                         '我们认为这可能成功')
        self.assertEqual(audit['reason'], 'polarity_conflict')
        self.assertEqual(audit['issues'], ['modal_polarity_disagreement'])

    def test_literal_negation_edits_are_reported_without_choosing_a_reading(self):
        for raw, refined in [('我们可以参加活动', '我们不可以参加活动。'),
                             ('我们没有收到通知', '我们收到通知。'),
                             ('我们可能需要参加活动', '我们可能不需要参加活动。'),
                             ('他们拒绝签字', '他们签字。')]:
            with self.subTest(raw=raw):
                audit = {}
                self.assertEqual(translation_source(raw, refined, audit=audit), raw)
                self.assertEqual(audit['issues'], ['negation_edit_disagreement'])

    def test_negation_synonyms_do_not_create_a_conflict(self):
        audit = {}
        self.assertEqual(translation_source('我们没收到通知', '我们没有收到通知。', audit=audit),
                         '我们没有收到通知。')
        self.assertEqual(audit['issues'], [])

    def test_translation_hints_do_not_apply_phonetic_source_replacements(self):
        with patch.dict(os.environ, {'MOZART_GLOSSARY': ''}):
            row = source_caption({'seq': 1, 'text': '元神出窍'}, 1, 'session')
        self.assertEqual(row['zh'], '元神出窍')

    def test_term_hints_and_direct_address_instruction_reach_the_model(self):
        answer = {'choices': [{'message': {'content': 'I am stunned, bro.'}, 'finish_reason': 'stop'}]}
        with patch('subtitle_bridge.request_json', return_value=answer) as request:
            list(caption_updates({'seq': 1, 'text': '我麻了兄弟'}, 1, 'session', 'translator'))
        prompt = request.call_args.args[1]['messages'][0]['content']
        self.assertIn('I am stunned', prompt)
        self.assertIn('Keep direct address as direct address', prompt)
        self.assertNotIn('Genshin Impact', prompt)

    def test_nullable_external_audits_allow_translation(self):
        answer = {'choices': [{'message': {'content': 'Hello.'}, 'finish_reason': 'stop'}]}
        with patch('subtitle_bridge.request_json', return_value=answer) as request:
            rows = list(caption_updates({'seq': 1, 'text': '你好', 'asr_numeric_audit': None,
                                        'asr_polarity_audit': None}, 1, 'session', 'translator'))
        request.assert_called_once()
        self.assertEqual(rows[-1]['translation_status'], 'completed')


class RefinementDeliveryTests(unittest.TestCase):
    def test_failed_and_skipped_refinement_wake_a_waiting_translation(self):
        for status in ('failed', 'skipped'):
            with self.subTest(status=status):
                rows, waiting, translated = [], threading.Event(), threading.Event()
                def translate(*args, **kwargs):
                    translated.set()
                    return 'Hello.', .1
                dispatcher = CaptionDispatcher(rows.append, 'session', 'translator', refine=True)
                wait = dispatcher.refined_ready.wait
                def observed_wait(timeout):
                    waiting.set()
                    return wait(timeout)
                with patch('subtitle_bridge.refinement_wait', return_value=60), \
                        patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                        patch.object(dispatcher.refined_ready, 'wait', side_effect=observed_wait):
                    try:
                        dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
                        self.assertTrue(waiting.wait(2))
                        dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '你好',
                                           'refinement_status': status, 'refinement_error': 'Model unavailable'})
                        self.assertTrue(translated.wait(2))
                    finally:
                        self.assertTrue(dispatcher.close(3))
                self.assertEqual(rows[-1]['translation_source_reason'], 'refinement_' + status)
                self.assertEqual(rows[-1]['translation_source_text'], '你好')
                self.assertGreaterEqual(rows[-1]['translation_refinement_wait_ms'], 0)

    def test_failed_inline_candidate_does_not_replace_online_text(self):
        dispatcher = CaptionDispatcher(lambda row: None, 'session', 'translator', refine=True)
        try:
            audit = {}
            text = dispatcher.await_refinement({'seq': 1, 'text': '你好',
                'refined_text': '完全不同的候选', 'refinement_status': 'failed'}, deadline=None, diagnostics=audit)
        finally:
            self.assertTrue(dispatcher.close(3))
        self.assertEqual(text, '你好')
        self.assertEqual(audit['translation_source_reason'], 'refinement_failed')

    def test_terminal_result_before_final_is_not_lost(self):
        rows, done = [], threading.Event()
        def publish(row):
            rows.append(row)
            if row.get('translation_status') == 'completed':
                done.set()
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)):
            dispatcher = CaptionDispatcher(publish, 'session', 'translator', refine=True)
            try:
                dispatcher.accept({'type': 'refined', 'seq': 3, 'text': '你好',
                                   'refinement_status': 'skipped'})
                dispatcher.accept({'type': 'final', 'seq': 3, 'text': '你好'})
                self.assertTrue(done.wait(2))
            finally:
                self.assertTrue(dispatcher.close(3))
        self.assertEqual(rows[-1]['translation_source_reason'], 'refinement_skipped')

    def test_refinement_audits_block_translation_and_speech(self):
        for key, issue in [('asr_numeric_audit', 'itn_quantity_disagreement'),
                           ('asr_polarity_audit', 'modal_polarity_disagreement')]:
            with self.subTest(key=key), patch('subtitle_bridge.request_json') as request:
                rows, done = [], threading.Event()
                def publish(row):
                    rows.append(row)
                    if row.get('translation_status') == 'failed':
                        done.set()
                dispatcher = CaptionDispatcher(publish, 'session', 'translator', 'speech', refine=True)
                try:
                    dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '我们收到了七千元',
                        'refined_text': '我们收到了7000元。', 'refinement_status': 'completed',
                        key: {'issues': [issue]}})
                    dispatcher.accept({'type': 'final', 'seq': 1, 'text': '我们收到了七千元'})
                    self.assertTrue(done.wait(2))
                finally:
                    self.assertTrue(dispatcher.close(3))
                request.assert_not_called()
                self.assertEqual(rows[-1]['speech_skipped_reason'], 'asr_refinement_uncertainty')
                self.assertIn(issue, rows[-1]['translation_source_audit']['issues'])

    def test_unrelated_refinement_audits_do_not_block_the_online_source(self):
        rows, done = [], threading.Event()
        def publish(row):
            rows.append(row)
            if row.get('translation_status') == 'completed':
                done.set()
        answer = {'choices': [{'message': {'content': 'We went to that new restaurant.'}, 'finish_reason': 'stop'}]}
        with patch('subtitle_bridge.request_json', return_value=answer) as request, \
                patch('subtitle_bridge.refinement_wait', return_value=60):
            dispatcher = CaptionDispatcher(publish, 'session', 'translator', refine=True)
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '我们一起去了那家新开的餐厅'})
                dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '我们一起去了那家新开的餐厅',
                    'refined_text': '完全不同的识别结果可能800元', 'refinement_status': 'completed',
                    'asr_numeric_audit': {'issues': ['itn_quantity_disagreement']}})
                self.assertTrue(done.wait(2))
            finally:
                self.assertTrue(dispatcher.close(3))
        request.assert_called_once()
        self.assertEqual(rows[-1]['translation_source_reason'], 'dissimilar_refinement')
        self.assertEqual(rows[-1]['translation_source_audit']['issues'], [])
        self.assertEqual(rows[-1]['refinement_numeric_audit']['issues'], ['itn_quantity_disagreement'])

    def test_invalid_glossary_during_selection_reports_a_failed_job(self):
        rows = []
        entered, release, failed, completed = (threading.Event() for _ in range(4))
        def publish(row):
            rows.append(row)
            if row.get('translation_status') == 'failed':
                failed.set()
            if row.get('translation_status') == 'completed':
                completed.set()
        def delayed_glossary():
            entered.set()
            self.assertTrue(release.wait(3))
            return translation_glossary()
        answer = {'choices': [{'message': {'content': 'Hello.'}, 'finish_reason': 'stop'}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'terms.json'
            path.write_text('{}')
            with patch.dict(os.environ, {'MOZART_GLOSSARY': str(path)}), \
                    patch('subtitle_bridge.translation_glossary', side_effect=delayed_glossary), \
                    patch('subtitle_bridge.request_json', return_value=answer) as request:
                dispatcher = CaptionDispatcher(publish, 'session', 'translator', refine=True)
                try:
                    dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '你好',
                        'refined_text': '你好。', 'refinement_status': 'completed'})
                    dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
                    self.assertTrue(entered.wait(2))
                    path.write_text('{')
                    release.set()
                    self.assertTrue(failed.wait(2))
                    self.assertIn('术语表不是合法 JSON', rows[-1]['translation_error'])
                    path.write_text('{}')
                    dispatcher.accept({'type': 'refined', 'seq': 2, 'text': '你好',
                        'refined_text': '你好。', 'refinement_status': 'completed'})
                    dispatcher.accept({'type': 'final', 'seq': 2, 'text': '你好'})
                    self.assertTrue(completed.wait(2))
                finally:
                    release.set()
                    self.assertTrue(dispatcher.close(3))
                request.assert_called_once()

    def test_bad_glossary_during_refined_delivery_reports_both_failures(self):
        rows, done = [], threading.Event()
        def publish(row):
            rows.append(row)
            if row.get('translation_status') == 'failed':
                done.set()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'terms.json'
            path.write_text('{}')
            with patch.dict(os.environ, {'MOZART_GLOSSARY': str(path)}), \
                    patch('subtitle_bridge.refinement_wait', return_value=60), \
                    patch('subtitle_bridge.request_json') as request:
                dispatcher = CaptionDispatcher(publish, 'session', 'translator', refine=True)
                try:
                    dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
                    path.write_text('{')
                    dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '你好',
                        'refined_text': '你好。', 'refinement_status': 'completed'})
                    self.assertTrue(done.wait(2))
                finally:
                    self.assertTrue(dispatcher.close(3))
                request.assert_not_called()
        self.assertEqual(rows[-1]['refinement_status'], 'failed')
        self.assertIn('术语表不是合法 JSON', rows[-1]['refinement_error'])
        self.assertIn('术语表不是合法 JSON', rows[-1]['translation_error'])

    def test_internal_selection_error_stops_the_worker_and_propagates(self):
        errors = []
        with patch('subtitle_bridge.translation_source', side_effect=TypeError('Selection program error')), \
                patch('threading.excepthook', side_effect=lambda args: errors.append(args.exc_value)):
            dispatcher = CaptionDispatcher(lambda row: None, 'session', 'translator', refine=True)
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好', 'refinement_status': 'disabled'})
            dispatcher.worker.join(2)
            self.assertFalse(dispatcher.worker.is_alive())
            with self.assertRaisesRegex(RuntimeError, 'Translation worker failed') as raised:
                dispatcher.close(3)
        self.assertIsInstance(raised.exception.__cause__, TypeError)
        self.assertEqual(len(errors), 1)
        self.assertTrue(dispatcher.aborted)

    def test_source_quantity_conflict_blocks_calls_even_without_refiner_audit(self):
        rows, done = [], threading.Event()
        def publish(row):
            rows.append(row)
            if row.get('translation_status') == 'failed':
                done.set()
        with patch('subtitle_bridge.request_json') as request:
            dispatcher = CaptionDispatcher(publish, 'session', 'translator', 'speech', refine=True)
            try:
                dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '我们收到了七千元',
                    'refined_text': '我们收到了700元。', 'refinement_status': 'completed'})
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '我们收到了七千元'})
                self.assertTrue(done.wait(2))
            finally:
                self.assertTrue(dispatcher.close(3))
        request.assert_not_called()
        self.assertEqual(rows[-1]['translation_source_reason'], 'quantity_conflict')
        self.assertEqual(rows[-1]['translation_status'], 'failed')

    def test_late_refinement_retains_the_selected_source(self):
        rows, done = [], threading.Event()
        def publish(row):
            rows.append(row)
            if row.get('translation_status') == 'completed':
                done.set()
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Chinese and English.', .1)):
            dispatcher = CaptionDispatcher(publish, 'session', 'translator', refine=True)
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '中文和英文',
                                   'refinement_status': 'disabled'})
                self.assertTrue(done.wait(2))
                dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '中文和英文',
                    'refined_text': '完全不同的复核文本', 'refinement_status': 'completed'})
            finally:
                self.assertTrue(dispatcher.close(3))
        self.assertEqual(rows[-1]['translation_source_text'], '中文和英文')
        self.assertEqual(rows[-1]['refined_zh'], '完全不同的复核文本')

    def test_queue_refinement_and_request_timings_are_separate(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        rows = []
        def translate(url, event, **kwargs):
            if event['seq'] == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return 'Hello.', .1
        def publish(row):
            rows.append(row)
            if row.get('seq') == 2 and row.get('translation_status') == 'completed':
                done.set()
        clock = [100.0]
        with patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                patch('subtitle_bridge.time.monotonic', side_effect=lambda: clock[0]):
            dispatcher = CaptionDispatcher(publish, 'session', 'translator')
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
                self.assertTrue(entered.wait(2))
                dispatcher.accept({'type': 'final', 'seq': 2, 'text': '你好'})
                clock[0] = 100.125
                release.set()
                self.assertTrue(done.wait(2))
            finally:
                release.set()
                self.assertTrue(dispatcher.close(3))
        self.assertEqual(rows[-1]['translation_queue_wait_ms'], 125)
        self.assertEqual(rows[-1]['translation_refinement_wait_ms'], 0)
        self.assertGreaterEqual(rows[-1]['translate_ms'], 0)


class OfflineTranslationOracleTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / 'rvc-golden/asr-selection-20261028'
        spec = importlib.util.spec_from_file_location('asr_offline_probe', root / 'probe.py')
        self.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.probe)
        self.rows = json.loads((root / 'results.json').read_text())['records']

    def test_original_checks_hold_and_record_9_semantics_fail(self):
        checks = self.probe.regression_checks(self.rows)
        for name in ('all_candidates_completed', 'record_8', 'record_16', 'record_17',
                     'record_39', 'record_41', 'record_42', 'record_43'):
            with self.subTest(name=name):
                self.assertTrue(checks[name])
        for seq in (8, 16, 17, 39, 41, 42, 43):
            with self.subTest(seq=seq):
                self.assertTrue(checks['semantic_record_' + str(seq)])
        self.assertFalse(checks['semantic_record_9'])
        self.assertFalse(all(checks.values()))

    def test_original_errors_still_fail_the_keyword_checks(self):
        for seq in (8, 16, 17, 39, 41, 42):
            with self.subTest(seq=seq):
                changed = copy.deepcopy(self.rows)
                row = next(row for row in changed if row['seq'] == seq)
                row['candidate'] = dict(row['baseline'])
                self.assertFalse(self.probe.regression_checks(changed)['record_' + str(seq)])
        changed = copy.deepcopy(self.rows)
        row = next(row for row in changed if row['seq'] == 43)
        row['candidate']['en'] = 'Unfortunately, the State refused our terms.'
        self.assertFalse(self.probe.regression_checks(changed)['record_43'])
        changed = copy.deepcopy(self.rows)
        changed[0]['candidate']['status'] = 'failed'
        self.assertFalse(self.probe.regression_checks(changed)['all_candidates_completed'])

    def test_semantic_counterexamples_fail_while_the_keyword_survives(self):
        # 关键词仍然命中时，语义断言必须单独拒绝改义译文。
        for seq, english in [(8, 'Genshin Impact is a movie.'),
                             (8, 'Genshin Impact is not a game.'),
                             (41, 'The government is powerless against this.'),
                             (42, "Unfortunately, we rejected the United Kingdom's terms."),
                             (43, 'Unfortunately, the United Kingdom approved our terms.')]:
            with self.subTest(seq=seq):
                changed = copy.deepcopy(self.rows)
                row = next(row for row in changed if row['seq'] == seq)
                row['candidate']['en'] = english
                checks = self.probe.regression_checks(changed)
                self.assertTrue(checks['record_' + str(seq)])
                self.assertFalse(checks['semantic_record_' + str(seq)])

    def test_semantic_counterexamples_fail_for_swapped_and_replaced_objects(self):
        for seq, english in [(16, 'Middle Eastern English.'),
                             (39, 'You are stunned brother.'),
                             (39, 'I am stunned by your brother.'),
                             (42, 'Sincere regret that the State refused our terms.')]:
            with self.subTest(seq=seq):
                changed = copy.deepcopy(self.rows)
                row = next(row for row in changed if row['seq'] == seq)
                row['candidate']['en'] = english
                self.assertFalse(self.probe.regression_checks(changed)['semantic_record_' + str(seq)])

    def test_correct_text_without_terminal_punctuation_passes_semantics(self):
        for seq, english in [(8, 'Genshin Impact is a game'),
                             (41, 'We are powerless against this'),
                             (42, 'Sincere regret that the United Kingdom refused our terms')]:
            with self.subTest(seq=seq):
                changed = copy.deepcopy(self.rows)
                row = next(row for row in changed if row['seq'] == seq)
                row['candidate']['en'] = english
                checks = self.probe.regression_checks(changed)
                self.assertTrue(checks['record_' + str(seq)])
                self.assertTrue(checks['semantic_record_' + str(seq)])

    def test_reserved_cases_are_checked_when_supplied_to_the_probe(self):
        contract = self.probe.semantic_contract()
        reserved = {case.id: {'status': 'completed', 'en': case.reference_en}
                    for case in contract.cases if case.seq is None}
        checks = self.probe.regression_checks(self.rows, contract, reserved)
        self.assertTrue(all(checks['semantic_' + key] for key in reserved))
        key = 'reserved_printer_quantity'
        reserved[key]['en'] = 'We bought four printers.'
        self.assertFalse(self.probe.regression_checks(self.rows, contract, reserved)['semantic_' + key])
        del reserved[key]
        self.assertFalse(self.probe.regression_checks(self.rows, contract, reserved)['semantic_' + key])

    def test_completed_fragment_fails_the_semantic_check(self):
        for english in ('We shall do what we must do.', 'We shall do what we must do'):
            with self.subTest(english=english):
                changed = copy.deepcopy(self.rows)
                row = next(row for row in changed if row['seq'] == 9)
                row['candidate']['en'] = english
                checks = self.probe.regression_checks(changed)
                self.assertFalse(checks['semantic_record_9'])
                self.assertFalse(all(checks.values()))


if __name__ == '__main__':
    unittest.main()
