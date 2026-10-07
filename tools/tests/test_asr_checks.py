import pathlib,sys,unittest
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from asr_checks import numeric_audit,refine_with_numeric_check,polarity_audit,boundary_fragment_audit

class Stream:
    def __init__(self):self.options={}
    def set_option(self,key,value):self.options[key]=value
    def accept_waveform(self,rate,pcm):self.rate,self.pcm=rate,pcm

class Recognizer:
    def __init__(self,texts):self.texts=iter(texts);self.streams=[]
    def create_stream(self):
        s=Stream();self.streams.append(s);return s
    def decode_stream(self,s):s.result=SimpleNamespace(text=next(self.texts))

class NumericRecognitionChecks(unittest.TestCase):
    def test_measured_boundary_fragments_are_flagged_without_replacement(self):
        for text in ['他不管对面是谁，不管自己啊在。','妈妈已经有点不相信了妈妈。']:
            audit=boundary_fragment_audit(text)
            self.assertEqual(audit['source_text'],text)
            self.assertIsNone(audit['replacement_transcript'])
            self.assertFalse(audit['context_resolution_verified'])
        for text in ['他不管自己在做什么。','妈妈已经有点不相信了，妈妈开始怀疑了。','妈妈不相信自己了。','妈妈不相信妈妈说的话。']:
            self.assertIsNone(boundary_fragment_audit(text),text)
    def test_measured_polarity_conflict_is_not_resolved(self):
        before='然后可能又因或者说直接输'
        after='不可能又赢或者说直接输'
        audit=polarity_audit(before,after)
        self.assertEqual(audit['issues'],['modal_polarity_disagreement'])
        self.assertEqual((audit['online_text'],audit['refined_text']),(before,after))
        self.assertFalse(audit['transcripts_exactly_aligned'])
    def test_matched_and_intentional_modal_cues_preserved(self):
        for text in ('可能赢','不可能赢','可能赢，不可能输','不可能，不可能'):
            self.assertEqual(polarity_audit(text,text)['issues'],[])
        self.assertIsNone(polarity_audit('不太可能赢','不可能赢'))
        self.assertIsNone(polarity_audit('妈妈开始怀疑','妈妈不相信'))
        self.assertIn('modal_polarity_disagreement',polarity_audit('可能，不可能','可能')['issues'])
    def test_measured_same_audio_amount_conflict(self):
        r=Recognizer(['700。','七千']);pcm=[.1,0]
        text,audit=refine_with_numeric_check(r,pcm)
        self.assertEqual(text,'700。')
        self.assertIn('itn_quantity_disagreement',audit['issues'])
        self.assertEqual([s.options for s in r.streams],[{'use_itn':'1'},{'use_itn':'0'}])
        self.assertTrue(all(s.pcm is pcm for s in r.streams))
    def test_clipped_or_stuttered_numerals_are_not_resolved(self):
        for formatted,literal in [('500。','千五千又是大几千'),('11万块','一一万块'),('7005000','七千五千')]:
            self.assertIn('ambiguous_literal_quantity',numeric_audit(formatted,literal)['issues'])
    def test_equivalent_amounts_and_years(self):
        for formatted,literal in [('7000元','七千元'),('11万块','十一万块'),('2022年','二零二二年'),('2万和3万','两万和三万'),('300元','三百元')]:
            self.assertEqual(numeric_audit(formatted,literal)['issues'],[],(formatted,literal))
    def test_ranges_and_intentional_repeated_amounts(self):
        self.assertEqual(numeric_audit('2万到3万','两三万')['issues'],[])
        self.assertEqual(numeric_audit('1000，1000','一千，一千')['issues'],[])
        self.assertIn('itn_quantity_disagreement',numeric_audit('1000，1000','一千')['issues'])
    def test_non_numeric_speech_does_not_add_inference(self):
        r=Recognizer(['妈妈不相信了。'])
        self.assertEqual(refine_with_numeric_check(r,[0]),('妈妈不相信了。',None))
        self.assertEqual(len(r.streams),1)
    def test_vague_thousands_are_not_exact_amounts(self):
        r=Recognizer(['大几千元'])
        self.assertEqual(refine_with_numeric_check(r,[0]),('大几千元',None))
    def test_numeric_check_failure_is_explicit(self):
        r=Recognizer(['700'])
        text,audit=refine_with_numeric_check(r,[0])
        self.assertEqual(text,'700');self.assertEqual(audit['issues'],['numeric_check_failed'])
    def test_online_amount_triggers_check_when_final_drops_it(self):
        r=Recognizer(['拿钱。','拿钱'])
        _,audit=refine_with_numeric_check(r,[0],'拿了一千块')
        self.assertIsNotNone(audit)
        self.assertFalse(audit['human_source_transcript_verified'])

if __name__=='__main__':unittest.main()
