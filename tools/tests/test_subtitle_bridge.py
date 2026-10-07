import pathlib,sys,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from subtitle_bridge import translate, translate_recognized_event

def answer(text,finish='stop'):
    return {'choices':[{'message':{'content':text},'finish_reason':finish}]}

class BridgeChecks(unittest.TestCase):
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
