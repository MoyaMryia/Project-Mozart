import pathlib,sys,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from subtitle_bridge import caption_updates, translate, translate_recognized_event

def answer(text,finish='stop'):
    return {'choices':[{'message':{'content':text},'finish_reason':finish}]}

class BridgeChecks(unittest.TestCase):
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

    def test_rejected_translation_remains_visible_and_requests_speech(self):
        for source, candidate, finish, reason in [
                ('这个叫跑分', 'This is called 跑分.', 'stop', 'untranslated Chinese'),
                ('要付12000元', 'Pay 1,000.', 'stop', 'numerals missing'),
                ('你好。', 'Hello and', 'length', 'token limit')]:
            with self.subTest(candidate=candidate), patch('subtitle_bridge.request_json',
                    side_effect=[answer(candidate, finish), answer(candidate, finish), {'id': 'speech-job'}]) as request:
                updates = list(caption_updates({'seq': 7, 'text': source},
                    1, 'session', 'http://example', 'http://speech'))
            self.assertEqual(len(updates), 3)
            self.assertEqual(updates[1]['en'], candidate)
            self.assertEqual(updates[1]['translation_status'], 'failed')
            self.assertIn(reason, updates[1]['translation_error'])
            self.assertEqual(updates[2]['speech'], 'speech-job')
            self.assertTrue(updates[2]['speech_degraded'])
            self.assertEqual(request.call_args.args[1]['text'], candidate)
            self.assertTrue(request.call_args.args[1]['speech_degraded'])

    def test_failed_retry_uses_previous_candidate_and_reports_speech_error(self):
        with patch('subtitle_bridge.request_json', side_effect=[answer('This is 跑分.'),
                TimeoutError('Translation timeout'), TimeoutError('Speech timeout')]):
            updates = list(caption_updates({'seq': 7, 'text': '这个叫跑分'},
                1, 'session', 'http://example', 'http://speech'))
        self.assertEqual(updates[-1]['en'], 'This is 跑分.')
        self.assertEqual(updates[-1]['translation_error'], 'Translation timeout')
        self.assertEqual(updates[-1]['speech_error'], 'Speech timeout')
        self.assertTrue(updates[-1]['speech_degraded'])

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
