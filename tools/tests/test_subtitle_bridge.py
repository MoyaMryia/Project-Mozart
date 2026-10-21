import json,os,pathlib,sys,tempfile,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from subtitle_bridge import (CaptionDispatcher, caption_updates, source_caption, translate,
    translate_recognized_event, translation_source)

def answer(text,finish='stop'):
    return {'choices':[{'message':{'content':text},'finish_reason':finish}]}

class RefinementSourceTests(unittest.TestCase):
    def test_broken_numeral_in_refined_text_keeps_streaming_text(self):
        raw = '充值了五百输光一千输光'
        self.assertEqual(translation_source(raw, '充值了500输光，一00输光'), raw)
        self.assertEqual(translation_source('他是二零二二年来的', '他是2022年来的'), '他是2022年来的')

    def test_short_fragment_keeps_streaming_text(self):
        self.assertEqual(translation_source('一起学习', '学习大。'), '一起学习')

    def test_unrelated_refinement_keeps_streaming_text(self):
        raw = '今天天气不错我们出去走走吧'
        self.assertEqual(translation_source(raw, '完全无关的另一句话内容'), raw)

    def test_close_refinement_replaces_streaming_text(self):
        raw = '你给人家种的礼物这电网贷的钱买的你不修课吗'
        refined = '你给人家送的礼物是借网贷的钱买的，你不羞愧吗？'
        self.assertEqual(translation_source(raw, refined), refined)

    def test_refinement_wait_scales_with_utterance_length(self):
        from subtitle_bridge import refinement_wait, REFINED_WAIT_MAX
        short = dict(seq=1, text='短句', audio_start_pts_ns=0, audio_end_pts_ns=1_000_000_000)
        long = dict(seq=1, text='长句', audio_start_pts_ns=0, audio_end_pts_ns=7_000_000_000)
        self.assertLess(refinement_wait(short), refinement_wait(long))
        self.assertEqual(refinement_wait(long), REFINED_WAIT_MAX)
        self.assertAlmostEqual(refinement_wait(short), 0.7, places=6)

    def test_missing_refinement_keeps_streaming_text(self):
        self.assertEqual(translation_source('这句话没有问题', None), '这句话没有问题')

    def test_caption_updates_translates_the_given_source(self):
        event = dict(seq=1, text='原始识别文本')
        with patch('subtitle_bridge.translate_recognized_event',
                   return_value=('A test.', .1)) as translate_call:
            list(caption_updates(event, 1, 'session', 'translator', translate_text='复核后的文本'))
        self.assertEqual(translate_call.call_args.args[1]['text'], '复核后的文本')


class RefinementOrderingTests(unittest.TestCase):
    def test_refined_text_wins_when_it_arrives_after_the_final(self):
        seen = []
        def fake_translate(url, event, audit=None):
            seen.append(event['text'])
            return 'A test.', .1
        dispatcher = CaptionDispatcher(lambda record: None, 'session', 'translator', refine=True)
        self.addCleanup(dispatcher.close)
        final = dict(type='final', seq=7, text='你给人家种的礼物这电网贷的钱买的你不修课吗')
        refined = dict(type='refined', seq=7, refinement_status='completed',
                       refined_text='你给人家送的礼物是借网贷的钱买的，你不羞愧吗？')
        with patch('subtitle_bridge.translate_recognized_event', side_effect=fake_translate):
            dispatcher.accept(final)
            time.sleep(.2)
            dispatcher.accept(refined)
            dispatcher.close()
        self.assertEqual(seen, [refined['refined_text']])

    def test_translation_proceeds_without_refinement_after_the_wait(self):
        seen = []
        def fake_translate(url, event, audit=None):
            seen.append(event['text'])
            return 'A test.', .1
        dispatcher = CaptionDispatcher(lambda record: None, 'session', 'translator', refine=True)
        self.addCleanup(dispatcher.close)
        final = dict(type='final', seq=8, text='没有复核结果时使用流式原文')
        with patch('subtitle_bridge.translate_recognized_event', side_effect=fake_translate):
            dispatcher.accept(final)
            dispatcher.close()
        self.assertEqual(seen, [final['text']])


class BridgeChecks(unittest.TestCase):
    def test_spelled_amount_keeps_original_candidate_without_retry(self):
        candidate = 'Then I downloaded a little bit of money that had a seven thousand limit.'
        with patch('subtitle_bridge.request_json', return_value=answer(candidate)) as request:
            result, _ = translate('translator', '然后我就下载了一点居然有七千的额度')
        self.assertEqual(result, candidate)
        self.assertEqual(request.call_count, 1)

    def test_speech_receives_observed_duration_and_source_timestamp(self):
        event = dict(seq=1, text='测试。', emitted_at=123.,
            audio_start_pts_ns=1_000_000_000, audio_end_pts_ns=3_000_000_000)
        with patch('subtitle_bridge.translate_recognized_event', return_value=('A test.', .1)), \
                patch('subtitle_bridge.request_json', return_value={'id':'job'}) as request:
            list(caption_updates(event, 1, 'session', 'translator', 'speech'))
        body = request.call_args.args[1]
        self.assertEqual(body['source_duration_seconds'], 2)
        self.assertEqual(body['source_emitted_at'], 123.)

    def test_age_title_repair_keeps_existing_translation_without_another_request(self):
        audit = []
        with patch('subtitle_bridge.request_json', return_value=answer(
                'Your mother is dancing with the lobster大妈 in the square dance.')) as request:
            result, _ = translate('translator', '你妈妈跟龙虾大妈跳广场舞', audit=audit)
        self.assertEqual(result, 'Your mother is dancing with the lobster older woman in the square dance.')
        self.assertEqual(request.call_count, 1)
        self.assertTrue(audit[0]['untranslated_chinese'])
        self.assertFalse(audit[1]['untranslated_chinese'])
        self.assertEqual(audit[1]['term_repair']['method'], 'common_age_title_glossary')

    def test_title_repair_does_not_accept_changed_numbers_or_unknown_terms(self):
        for source, candidate in [('大妈付12000元', '大妈 paid 1000.'),
                                  ('这个叫跑分', 'This is called 跑分.'),
                                  ('年轻女人', 'The 大妈 dances.')]:
            with self.subTest(source=source), patch('subtitle_bridge.request_json', return_value=answer(candidate)) as request:
                with self.assertRaises(ValueError):
                    translate('translator', source)
            self.assertEqual(request.call_count, 2)

    def test_context_copy_retries_without_previous_source_or_translation(self):
        audit=[]
        context=[{'source':'最后的结果一定是输','translation':'The result is a loss.'}]
        with patch('subtitle_bridge.request_json',side_effect=[answer('The result is a loss.'),answer('Nobody can escape this cycle.')]) as request:
            result,_=translate('translator','没有人能逃掉这个循环',context=context,audit=audit)
        self.assertEqual(result,'Nobody can escape this cycle.')
        self.assertTrue(audit[0]['repeated_context_translation'])
        first,retry=[c.args[1]['messages'][0]['content'] for c in request.call_args_list]
        self.assertIn('最后的结果一定是输',first)
        self.assertNotIn('The result is a loss.',first)
        self.assertNotIn('最后的结果一定是输',retry)

    def test_rejected_translation_remains_visible_without_speech(self):
        for source, candidate, finish, reason in [
                ('这个叫跑分', 'This is called 跑分.', 'stop', 'untranslated Chinese'),
                ('要付12000元', 'Pay 1,000.', 'stop', 'numerals missing'),
                ('你好。', 'Hello and', 'length', 'token limit')]:
            with self.subTest(candidate=candidate), patch('subtitle_bridge.request_json',
                    side_effect=[answer(candidate, finish), answer(candidate, finish)]) as request:
                updates = list(caption_updates({'seq': 7, 'text': source},
                    1, 'session', 'http://example', 'http://speech'))
            self.assertEqual(len(updates), 2)
            self.assertEqual(updates[1]['en'], candidate)
            self.assertEqual(updates[1]['translation_status'], 'failed')
            self.assertIn(reason, updates[1]['translation_error'])
            self.assertEqual(updates[1]['speech_skipped_reason'], 'translation_failed')
            self.assertEqual(request.call_count, 2)
            self.assertTrue(all(c.args[0].endswith('/v1/chat/completions') for c in request.call_args_list))

    def test_failed_retry_keeps_previous_candidate_without_speech(self):
        with patch('subtitle_bridge.request_json', side_effect=[answer('This is 跑分.'),
                TimeoutError('Translation timeout')]) as request:
            updates = list(caption_updates({'seq': 7, 'text': '这个叫跑分'},
                1, 'session', 'http://example', 'http://speech'))
        self.assertEqual(updates[-1]['en'], 'This is 跑分.')
        self.assertEqual(updates[-1]['translation_error'], 'Translation timeout')
        self.assertEqual(updates[-1]['speech_skipped_reason'], 'translation_failed')
        self.assertEqual(request.call_count, 2)

    def test_internal_translation_error_propagates(self):
        with patch('subtitle_bridge.translate_recognized_event', side_effect=RuntimeError('model bug')):
            with self.assertRaisesRegex(RuntimeError, 'model bug'):
                list(caption_updates({'seq': 7, 'text': '你好。'}, 1, 'session', 'translator', 'speech'))

    def test_term_loop_retries_with_source_count_and_keeps_legitimate_pf(self):
        source = 'PF准备好了。PF是什么？PF可以吗？你懂PF吗？'
        valid = 'PF is ready. What is PF? Is PF possible? Do you understand PF?'
        audit = []
        with patch('subtitle_bridge.request_json', side_effect=[answer('PF, '*20), answer(valid)]) as request:
            result, _ = translate('translator', source, audit=audit)
        self.assertEqual(result, valid)
        self.assertEqual(audit[0]['repeated_source_terms'][0]['source_count'], 4)
        self.assertIn('PF=4', request.call_args.args[1]['messages'][0]['content'])

    def test_term_loop_never_enters_speech(self):
        with patch('subtitle_bridge.request_json', return_value=answer('PF, '*20)) as request:
            updates = list(caption_updates({'seq': 7, 'text': '你懂PF吗？'}, 1, 'session', 'translator', 'speech'))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(updates[-1]['translation_status'], 'failed')
        self.assertIn('repeated source terms', updates[-1]['translation_error'])
        self.assertNotIn('speech', updates[-1])

    def test_source_duration_does_not_request_translation_compression(self):
        event = dict(text='测试。', audio_start_pts_ns=1_000_000_000, audio_end_pts_ns=3_500_000_000)
        with patch('subtitle_bridge.request_json', return_value=answer('A test.')) as request:
            translate_recognized_event('translator', event)
        prompt = request.call_args.args[1]['messages'][0]['content']
        self.assertNotIn('2.50 seconds', prompt)
        self.assertNotIn('time budget', prompt)

    def test_glossary_follows_only_observed_source_terms(self):
        with patch('subtitle_bridge.request_json', return_value=answer('An anesthetic.')) as request:
            translate('translator', '这就是麻醉剂。')
        prompt = request.call_args.args[1]['messages'][0]['content']
        self.assertIn('anesthetic', prompt)
        self.assertNotIn('older woman', prompt)
        with patch('subtitle_bridge.request_json', return_value=answer('DNA is a nucleic acid.')) as request:
            translate('translator', 'DNA是一种核酸。')
        self.assertNotIn('anesthetic', request.call_args.args[1]['messages'][0]['content'])

    def test_caption_shows_corrected_term_and_keeps_asr_text(self):
        directory = tempfile.TemporaryDirectory()
        path = pathlib.Path(directory.name)/'glossary.json'
        path.write_text(json.dumps({'网赌': 'online gambling'}), encoding='utf-8')
        with patch.dict(os.environ, {'MOZART_GLOSSARY': str(path)}):
            record = source_caption({'text': '现在就碰到王独的人', 'seq': 3, 'type': 'final'}, 3, 'session')
        self.assertEqual(record['zh'], '现在就碰到网赌的人')
        self.assertEqual(record['zh_raw'], '现在就碰到王独的人')
        self.assertEqual(record['homophone_fixes'], [{'from': '王独', 'to': '网赌'}])
        directory.cleanup()

    def test_domain_terms_reach_the_prompt_only_when_present(self):
        directory = tempfile.TemporaryDirectory()
        path = pathlib.Path(directory.name)/'glossary.json'
        path.write_text(json.dumps({'网贷': 'online lending', '网赌': 'online gambling'}), encoding='utf-8')
        with patch.dict(os.environ, {'MOZART_GLOSSARY': str(path)}):
            with patch('subtitle_bridge.request_json', return_value=answer('He borrowed through an app.')) as request:
                translate('translator', '他先从网贷借钱。')
            prompt = request.call_args.args[1]['messages'][0]['content']
            self.assertIn('online lending', prompt)
            self.assertNotIn('online gambling', prompt)
        directory.cleanup()

    def test_unavailable_translator_keeps_source_and_error_without_candidate(self):
        with patch('subtitle_bridge.request_json', side_effect=TimeoutError('Translation timeout')) as request:
            updates = list(caption_updates({'seq': 7, 'text': '你好。'},
                1, 'session', 'http://example', 'http://speech'))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[-1]['zh'], '你好。')
        self.assertEqual(updates[-1]['en'], '')
        self.assertEqual(updates[-1]['translation_error'], 'Translation timeout')

    def test_source_is_available_before_translation_starts(self):
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)) as translate_call:
            updates = caption_updates({'seq': 7, 'text': '你好。'}, 1, 'session', 'http://example')
            source = next(updates)
            translate_call.assert_not_called()
            self.assertEqual((source['zh'], source['en']), ('你好。', ''))
            self.assertEqual(source['translation_status'], 'pending')
            translated = next(updates)
            self.assertEqual(translated['utterance_id'], source['utterance_id'])
            self.assertEqual((source['revision'], translated['revision']), (0, 1))
            self.assertEqual(translated['en'], 'Hello.')
            self.assertEqual(translated['translation_status'], 'completed')
            self.assertEqual(source['en'], '')
            with self.assertRaises(StopIteration):
                next(updates)

    def test_translation_is_available_before_speech_request(self):
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)), \
                patch('subtitle_bridge.request_json', side_effect=TimeoutError('Speech timeout')) as speech_call:
            updates = caption_updates({'seq': 7, 'text': '你好。'}, 1, 'session',
                                      'http://example', 'http://speech')
            next(updates)
            translated = next(updates)
            speech_call.assert_not_called()
            self.assertEqual(translated['en'], 'Hello.')
            result = next(updates)
            self.assertEqual(result['revision'], 2)
            self.assertEqual(result['en'], 'Hello.')
            self.assertEqual(result['speech_error'], 'Speech timeout')
            self.assertNotIn('speech_error', translated)
            speech_call.assert_called_once()
            with self.assertRaises(StopIteration):
                next(updates)

    def test_uncertain_source_remains_visible_without_speech(self):
        event = {'seq': 7, 'text': '700。', 'asr_numeric_audit': {'issues': ['itn_quantity_disagreement']}}
        with patch('subtitle_bridge.request_json') as request:
            updates = list(caption_updates(event, 1, 'session', 'http://example', 'http://speech'))
        request.assert_not_called()
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]['zh'], '700。')
        self.assertEqual(updates[1]['translation_status'], 'failed')
        self.assertEqual(updates[1]['speech_skipped_reason'], 'asr_numeric_uncertainty')
        self.assertIn('Recognition uncertain', updates[1]['translation_error'])
        self.assertEqual(updates[1]['en'], '')

    def test_unresolved_context_never_calls_translator(self):
        for text in ['他不管对面是谁，不管自己啊在。','妈妈已经有点不相信了妈妈。']:
            event={'text':text}
            with patch('subtitle_bridge.request_json') as request:
                with self.assertRaisesRegex(ValueError,'Source context uncertain'):
                    translate_recognized_event('http://example',event)
            request.assert_not_called()
            self.assertEqual(event['text'],text)

    def test_measured_roles_are_retried_and_never_silently_accepted(self):
        audit=[]
        with patch('subtitle_bridge.request_json',side_effect=[answer('Mom doubts herself.'),answer('Mom is skeptical.')]) as request:
            self.assertEqual(translate('http://example','妈妈不相信了妈妈。',audit=audit)[0],'Mom is skeptical.')
        self.assertEqual(audit[0]['explicit_role_issues'],['unsupported_mother_reflexive'])
        self.assertIn('no explicit reflexive',request.call_args.args[1]['messages'][0]['content'])
        with patch('subtitle_bridge.request_json',return_value=answer('It does not matter whether I am there.')):
            with self.assertRaisesRegex(ValueError,'explicit source roles'):
                translate('http://example','他不管对面是谁。')

    def test_composite_amount_and_repeat_loss_never_pass(self):
        for source,output in [('不是七万五千','Not 70,000.'),('给1000，再给1000','Give 1,000.')]:
            with patch('subtitle_bridge.request_json',return_value=answer(output)):
                with self.assertRaisesRegex(ValueError,'numerals missing'):
                    translate('http://example',source)

    def test_polarity_conflict_never_requests_translation(self):
        event={'text':'不可能又赢','online_text':'然后可能又因',
               'asr_polarity_audit':{'issues':['modal_polarity_disagreement']}}
        with patch('subtitle_bridge.request_json') as request:
            with self.assertRaisesRegex(ValueError,'Recognition uncertain'):
                translate_recognized_event('http://example',event)
        request.assert_not_called()
    def test_matched_polarity_can_be_translated(self):
        with patch('subtitle_bridge.request_json',return_value=answer('It is impossible.')):
            self.assertEqual(translate_recognized_event('http://example',{'text':'不可能',
                'asr_polarity_audit':{'issues':[]}})[0],'It is impossible.')
    def test_uncertain_recognition_never_requests_translation(self):
        event={'text':'700。','asr_numeric_audit':{'issues':['itn_quantity_disagreement'],
               'literal_text':'七千'}}
        with patch('subtitle_bridge.request_json') as request:
            with self.assertRaisesRegex(ValueError,'Recognition uncertain'):
                translate_recognized_event('http://example',event)
        request.assert_not_called()
        self.assertEqual(event['text'],'700。')

    def test_agreed_numeric_reading_can_be_translated(self):
        with patch('subtitle_bridge.request_json',return_value=answer('7,000 yuan.')):
            result,_=translate_recognized_event('http://example',{'text':'七千元',
                'asr_numeric_audit':{'issues':[]}})
        self.assertEqual(result,'7,000 yuan.')

    def test_retry_does_not_anchor_rejected_answer(self):
        audit=[]
        with patch('subtitle_bridge.request_json',side_effect=[answer('Pay 110,000. Pay another 110,000.'),answer('Pay 110,000.')]) as call:
            result,_=translate('http://example','要付11万块',audit=audit)
        self.assertEqual(result,'Pay 110,000.')
        retry=call.call_args_list[1].args[1]['messages']
        self.assertEqual([m['role'] for m in retry],['system','user'])
        self.assertEqual(retry[1]['content'],'要付110000块')
        self.assertEqual(audit[0]['repeated_numbers'],['110000'])

    def test_truncated_output_never_becomes_speech(self):
        with patch('subtitle_bridge.request_json',return_value=answer('Pay 110,000 and','length')):
            with self.assertRaisesRegex(ValueError,'token limit'):translate('http://example','要付11万块')

    def test_calendar_and_context_specific_glossary(self):
        with patch('subtitle_bridge.request_json',return_value=answer('Account provision means providing accounts for online gambling platforms.')) as call:
            translate('http://example','跑分就是给网赌平台提供账户')
        self.assertIn('account provision',call.call_args.args[1]['messages'][1]['content'])
        with patch('subtitle_bridge.request_json',return_value=answer('The phone benchmark is fast.')) as call:
            translate('http://example','手机跑分很快')
        self.assertNotIn('account provision',str(call.call_args.args[1]['messages']))

    def test_no_irrelevant_examples_contaminate_prompt(self):
        with patch('subtitle_bridge.request_json',return_value=answer('Summer vacation.')) as call:
            translate('http://example','暑假')
        prompt=call.call_args.args[1]['messages'][0]['content']
        self.assertNotIn('second-tier',prompt)
        self.assertNotIn('70,000',prompt)

    def test_added_amounts_and_untranslated_output_rejected(self):
        for source,output,reason in [('大几千，700','Over 70,000 and 700.','added amounts'),('这个叫跑分','This is called 跑分.','untranslated Chinese')]:
            with patch('subtitle_bridge.request_json',return_value=answer(output)):
                with self.assertRaisesRegex(ValueError,reason):translate('http://example',source)

    def test_added_negation_absent_from_the_source_is_rejected(self):
        # 实测样本：然后我就下载了一点 => I downloaded a little bit, but it's not enough.
        source = '然后我就下载了一点'
        with patch('subtitle_bridge.request_json', return_value=answer("I downloaded a little bit, but it's not enough.")):
            with self.assertRaisesRegex(ValueError, 'added negation'):
                translate('http://example', source)
        with patch('subtitle_bridge.request_json', return_value=answer('I downloaded a little.')):
            self.assertEqual(translate('http://example', source)[0], 'I downloaded a little.')

    def test_source_negation_does_not_block_negated_translation(self):
        source = '刚成年为什么不能用三叉零借条'
        output = 'Why a new adult cannot use the pledge.'
        with patch('subtitle_bridge.request_json', return_value=answer(output)):
            self.assertEqual(translate('http://example', source)[0], output)

    def test_value_cannot_be_spoken_as_item_count(self):
        source='我的库存里有等值的商品38000多。'
        with patch('subtitle_bridge.request_json',return_value=answer('My inventory has 38,000+ items.')):
            with self.assertRaisesRegex(ValueError,'value changed to item count'):translate('http://example',source)
        with patch('subtitle_bridge.request_json',return_value=answer('My inventory has goods worth over 38,000.')):
            self.assertEqual(translate('http://example',source)[0],'My inventory has goods worth over 38,000.')


    def test_repayment_failure_cannot_enter_speech(self):
        audit=[]
        with patch('subtitle_bridge.request_json',return_value=answer('Top up and double, then take a loan.')) as call:
            with self.assertRaisesRegex(ValueError,'loan repayment'):
                translate('http://example','充值翻倍还贷款。然后。',audit=audit)
        self.assertTrue(audit[0]['loan_repayment_changed'])
        self.assertIn('repay the loan',call.call_args.args[1]['messages'][1]['content'])

if __name__=='__main__':unittest.main()
