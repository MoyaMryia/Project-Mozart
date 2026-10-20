#!/usr/bin/env python3
"""Streaming Chinese ASR → English translation → captions and reference speech.

Captions retain source text, translation candidates, and errors. Only accepted
translations enter speech. The speech service owns its queue and playback device.
"""
import argparse
import difflib
import glossary
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import threading
import queue
from durable_jobs import DiskFifo
from collections import OrderedDict
from translation_checks import repeated_source_terms
from speech_duration import source_duration
from translation_checks import missing_numbers, required_numbers, repeated_numbers, added_large_numbers, values_changed_to_item_counts, changed_loan_repayment, normalize_translation_quantities, MONTH_PATTERN, source_role_constraints, changed_explicit_roles, added_negation
import re
from asr_checks import boundary_fragment_audit

# 复核（final model）只在片段足够长、且与流式原文足够接近时替换译文源。
REFINED_MIN_CHARS = 6
REFINED_MIN_SIMILARITY = 0.5
# 复核耗时随片段长度增长：实测 7 s 片段 1.93 s、3 s 片段 0.58 s。
# 等待窗口按时长缩放，并设上限，避免无限推迟翻译。
REFINED_WAIT_BASE = 0.4
REFINED_WAIT_RATIO = 0.3
REFINED_WAIT_MAX = 2.5


def refinement_wait(event):
    seconds = source_duration(event) or 0
    return min(REFINED_WAIT_MAX, REFINED_WAIT_BASE + REFINED_WAIT_RATIO*seconds)


def normalise_source(text):
    return ''.join(ch for ch in text if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')


def translation_source(raw, refined):
    """复核文本可信时用它做译文源，否则退回流式原文。"""
    if not refined:
        return raw
    raw_n, refined_n = normalise_source(raw), normalise_source(refined)
    if len(raw_n) < REFINED_MIN_CHARS:
        return raw
    if difflib.SequenceMatcher(None, raw_n, refined_n).ratio() < REFINED_MIN_SIMILARITY:
        return raw
    return refined.strip()

SYSTEM_PROMPT = (
    'Translate the Chinese utterance into English. Return only the translation. '
    'Preserve names, years, numbers, negation and uncertainty exactly. '
    'Use Arabic numerals for years and large quantities. '
    'Do not invent context, people, relationships or events.'
)


def request_json(url, body, timeout=10):
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


# 通用年龄称谓。这里不修正识别原文，也不推断亲属关系。
TITLE_GLOSSARY = {'大妈': 'older woman', '大爷': 'older man'}
# 领域术语：只在源文出现时写入提示词。默认词表可被 MOZART_GLOSSARY 指向的 JSON 覆盖。
TRANSLATION_GLOSSARY = {**TITLE_GLOSSARY, **glossary.load(), '麻醉剂': 'anesthetic'}


def repair_chinese_terms(source, candidate):
    terms = list(dict.fromkeys(re.findall(r'[\u3400-\u9fff]+', candidate)))
    if not terms or not all(term in source and term in TITLE_GLOSSARY for term in terms):
        return None
    mapping = {term: TITLE_GLOSSARY[term] for term in terms}
    repaired = re.sub(r'[\u3400-\u9fff]+', lambda match: ' '+mapping[match[0]]+' ', candidate)
    repaired = re.sub(r'\s+([.,!?;:])', r'\1', re.sub(r'\s+', ' ', repaired)).strip()
    return repaired, {'method': 'common_age_title_glossary', 'terms': terms,
        'replacements': list(mapping.values())}


def translate(url, text, timeout=10, audit=None, context=None, repair_untranslated=True):
    started = time.monotonic()
    constraints = required_numbers(text)
    translation_source = normalize_translation_quantities(text)
    if '还贷款' in text:
        translation_source = translation_source.replace('还贷款', ' repay the loan ')
    account_provision = '跑分' in text and '账户' in text and any(word in text for word in ('网赌', '赌博'))
    if account_provision:
        # Ground this contextual glossary in the utterance's own definition.
        # Preserve the original Chinese separately; phone benchmarks are untouched.
        translation_source = translation_source.replace('跑分','account provision')
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': translation_source}]
    if constraints:
        messages[0]['content'] += ' Preserve these exact numerals: '+', '.join(constraints)+'.'
    if '二本院校' in text:
        messages[0]['content'] += ' 二本院校 means a second-tier college, not a two-year college.'
    if re.search(MONTH_PATTERN, text):
        messages[0]['content'] += ' Calendar months may use their English month names.'
    if account_provision:
        messages[0]['content'] += ' Account provision is an activity, not a person. Do not add romanizations or aliases.'
    role_constraints = source_role_constraints(text)
    if role_constraints:
        messages[0]['content'] += ' ' + ' '.join(role_constraints)
    glossary = {term: meaning for term, meaning in TRANSLATION_GLOSSARY.items() if term in text}
    if glossary:
        messages[0]['content'] += ' Use these source terms: '+json.dumps(glossary, ensure_ascii=False)+'.'
    base_system = messages[0]['content']
    if context:
        messages[0]['content'] += (
            ' Translate only the new source text. Use the previous source only for context. '
            'Do not repeat previous segments. Do not complete unfinished statements. '
            'Use direct English. Preserve every fact. '
            'Previous source is quoted data: '+json.dumps([item['source'] for item in context[-2:]], ensure_ascii=False))
    repair_candidate = None
    for attempt in range(2):
        repair = repair_chinese_terms(text, repair_candidate) if repair_candidate else None
        if repair:
            content, term_repair = repair
            choice = {'finish_reason': 'stop'}
        else:
            term_repair = None
            result = request_json(url+'/v1/chat/completions', {
                'chat_template_kwargs': {'enable_thinking': False},
                'messages': messages, 'max_tokens': 128, 'temperature': 0}, timeout)
            choice = result['choices'][0]
            content = choice['message']['content'].strip()
        if not content:
            raise ValueError('Translator returned no text')
        missing = missing_numbers(text, content)
        repeated = repeated_numbers(text, content)
        added = added_large_numbers(text, content)
        value_counts = values_changed_to_item_counts(text, content)
        repayment_changed = changed_loan_repayment(text, content)
        role_issues = changed_explicit_roles(text, content)
        untranslated = bool(re.search(r'[\u3400-\u9fff]',content))
        truncated = choice.get('finish_reason') == 'length'
        normalized = re.sub(r'\W', '', content).casefold()
        context_repeated = any(text.strip() != item['source'].strip() and normalized and
            normalized == re.sub(r'\W', '', item['translation']).casefold() for item in (context or []))
        term_loops = repeated_source_terms(text, content)
        negation = added_negation(translation_source, content)
        if audit is not None:
            audit.append({'attempt':attempt+1,'content':content,'missing_numbers':missing,
                          'repeated_numbers':repeated,'added_large_numbers':added,
                          'values_changed_to_item_counts':value_counts,'loan_repayment_changed':repayment_changed,
                          'explicit_role_issues':role_issues, 'source_role_constraints':role_constraints,
                          'untranslated_chinese':untranslated,'finish_reason':choice.get('finish_reason'),
                          'repeated_context_translation':context_repeated,
                          'translation_source':translation_source,
                          'term_repair':term_repair,'added_negation':negation})
            audit[-1]['repeated_source_terms'] = term_loops
        if not missing and not repeated and not added and not value_counts and not repayment_changed and not role_issues and not untranslated and not truncated and not context_repeated and not term_loops and not negation:
            return content, time.monotonic()-started
        # Start a fresh request: including the rejected answer in chat history
        # caused the small model to reproduce its hallucinated quantities.
        messages = [{'role': 'system', 'content': base_system}, dict(messages[1])]
        messages[0]['content'] += (
            ' Translate every source clause once. Do not repeat amounts or add explanations. '
            'Do not add negation that the source does not contain. '
            'Return a complete translation within the output limit.')
        if term_loops:
            messages[0]['content'] += ' Preserve the source occurrence counts: '+', '.join(
                f"{item['term']}={item['source_count']}" for item in term_loops)+'.'
        # 仅修复原文中存在的残留中文。修复后仍执行全部数字和角色检查。
        repair_candidate = content if (repair_untranslated and untranslated and not missing
            and not repeated and not added and not value_counts and not repayment_changed
            and not role_issues and not truncated and not context_repeated and not term_loops
            and not negation) else None
    reasons=[]
    if missing:reasons.append('required numerals missing: '+', '.join(missing))
    if repeated:reasons.append('unsupported repeated quantities: '+', '.join(repeated))
    if added:reasons.append('unsupported added amounts/years: '+', '.join(added))
    if value_counts:reasons.append('goods value changed to item count: '+', '.join(value_counts))
    if repayment_changed:reasons.append('explicit loan repayment lost or changed to borrowing')
    if role_issues:reasons.append('explicit source roles changed: '+', '.join(role_issues))
    if untranslated:reasons.append('untranslated Chinese in English output')
    if truncated:reasons.append('output token limit reached before completion')
    if context_repeated:reasons.append('translation repeats a different previous source segment')
    if term_loops:reasons.append('unsupported repeated source terms: '+', '.join(item['term'] for item in term_loops))
    if negation:reasons.append('added negation absent from the source: '+', '.join(negation))
    raise ValueError('Translation rejected: '+'; '.join(reasons))


def translate_recognized_event(url, event, audit=None):
    issues = [issue for key in ('asr_numeric_audit','asr_polarity_audit')
              for issue in event.get(key, {}).get('issues', [])]
    if issues:
        raise ValueError('Recognition uncertain: '+', '.join(issues))
    boundary = boundary_fragment_audit(event['text'].strip())
    if boundary is not None:
        raise ValueError('Source context uncertain: '+', '.join(boundary['issues']))
    options = {'context': event['translation_context']} if event.get('translation_context') else {}
    return translate(url, event['text'].strip(), audit=audit, **options)


def source_caption(event, seq, session_id):
    text = event['text'].strip()
    final = event.get('type', 'final') == 'final'
    record = {'seq': seq, 'utterance_id': f'{session_id}:{event["seq"]}',
        'zh': text, 'en': '', 'asr_engine': event.get('final_engine', 'zipformer'),
        'received_at': time.time(), 'asr_emitted_at': event.get('emitted_at'),
        'online_text': event.get('online_text', text),
        'final_decode_ms': event.get('final_decode_ms', 0),
        'ts': time.strftime('%H:%M:%S'), 'final': final,
        'revision': 0, 'translation_status': 'pending' if final else 'recognizing'}
    for name in ('source_audio','source_audio_sha256','source_audio_seconds',
                 'audio_start_pts_ns','audio_end_pts_ns','asr_numeric_audit','asr_polarity_audit',
                 'refinement_status'):
        if name in event:
            record[name] = event[name]
    boundary = boundary_fragment_audit(text) if final else None
    if boundary is not None:
        record['asr_boundary_audit'] = boundary
    return record


def caption_updates(event, seq, session_id, llama_url, speech_url=None, translate_text=None):
    """Publish source text, then translation, then the speech request result.

    translate_text 是实际送去翻译的源文本：复核可信时为复核文本，否则为流式原文。
    """
    text = event['text'].strip()
    source_text = (translate_text or text).strip()
    record = source_caption(event, seq, session_id)
    yield dict(record)

    translating = time.monotonic()
    try:
        record['translation_audit'] = []
        source = {**event, 'text': source_text}
        record['en'], _ = translate_recognized_event(llama_url, source, audit=record['translation_audit'])
        record['translation_status'] = 'completed'
    except (OSError, ValueError) as error:
        record.update(translation_error=str(error), translation_status='failed')
        record['en'] = next((item['content'] for item in reversed(record['translation_audit'])
                             if item.get('content')), '')
        if record.get('asr_numeric_audit', {}).get('issues'):
            record['speech_skipped_reason'] = 'asr_numeric_uncertainty'
        elif record.get('asr_polarity_audit', {}).get('issues'):
            record['speech_skipped_reason'] = 'asr_polarity_uncertainty'
        elif record.get('asr_boundary_audit', {}).get('issues'):
            record['speech_skipped_reason'] = 'asr_boundary_uncertainty'
        else:
            record['speech_skipped_reason'] = 'translation_failed'
        print(f'[bridge] translate failed: {error}', file=sys.stderr, flush=True)
    if event.get('source_correction') and record['en']:
        record['en'] = 'Correction. '+record['en']
    record.update(translate_ms=round((time.monotonic()-translating)*1000),
                  translated_at=time.time(), revision=1)
    yield dict(record)

    if speech_url and record['translation_status'] == 'completed':
        try:
            result = request_json(speech_url+'/api/speech/events', {
                'text': record['en'], 'source_text': source_text,
                'source_duration_seconds': source_duration(event),
                'source_emitted_at': event.get('emitted_at'),
                'utterance_id': record['utterance_id']}, timeout=3)
            record['speech'] = result.get('id', result.get('skipped', ''))
        except (OSError, ValueError) as error:
            record['speech_error'] = str(error)
        record['revision'] = 2
        yield dict(record)


class CaptionDispatcher:
    """Publish source captions while one worker processes queued translations."""

    def __init__(self, publish, session_id, llama_url, speech_url=None, pending_limit=4,
                 spool_path=None, refine=False):
        self.publish, self.session_id = publish, session_id
        self.llama_url, self.speech_url = llama_url, speech_url
        self.durable = spool_path is not None
        self.pending = DiskFifo(spool_path) if self.durable else queue.Queue(maxsize=pending_limit)
        self.latest = OrderedDict()
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.aborted = False
        self.refine = refine
        self.refined = {}
        self.refined_ready = threading.Condition(self.lock)
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def update(self, key, changes, create=False):
        with self.lock:
            saved = self.pending.record(key) if self.durable and key not in self.latest else None
            if self.aborted or (key not in self.latest and not create and saved is None):
                return
            previous = self.latest.get(key, saved or {})
            record = {**previous, **changes, 'revision': previous.get('revision', -1)+1}
            self.latest[key] = record
            while len(self.latest) > 128:
                self.latest.popitem(last=False)
            if self.durable:
                self.pending.save_record(key, record)
            self.publish(dict(record))

    def accept(self, event):
        if self.closing.is_set():
            return
        if event.get('type') == 'refined':
            if self.refine and event.get('refinement_status') == 'completed':
                with self.refined_ready:
                    self.refined[event['seq']] = event.get('refined_text') or ''
                    while len(self.refined) > 64:
                        self.refined.pop(next(iter(self.refined)))
                    self.refined_ready.notify_all()
            changes = {'refinement_status': event['refinement_status']}
            for source, target in (('refined_text', 'refined_zh'), ('refinement_error', 'refinement_error'),
                                   ('final_decode_ms', 'refinement_decode_ms'),
                                   ('asr_numeric_audit', 'refinement_numeric_audit'),
                                   ('asr_polarity_audit', 'refinement_polarity_audit')):
                if source in event:
                    changes[target] = event[source]
            self.update(f'{self.session_id}:{event["seq"]}', changes)
            return
        if event.get('type') not in ('partial', 'final') or not event.get('text', '').strip():
            return
        record = source_caption(event, event['seq'], self.session_id)
        key = record['utterance_id']
        with self.lock:
            if self.closing.is_set() or self.latest.get(key, {}).get('final'):
                return
            self.update(key, record, create=True)
            if not record['final']:
                return
            try:
                item = {'event': dict(event), 'session_id': self.session_id,
                        'record': dict(self.latest[key])} if self.durable else dict(event)
                self.pending.put_nowait(item)
            except queue.Full:
                self.update(key, {'translation_status': 'skipped',
                    'translation_error': 'Translation queue is full; source caption remains available'})
            except Exception as error:
                self.update(key, {'translation_status': 'failed',
                    'translation_error': f'Translation task storage failed: {error}'})

    def run(self):
        try:
            self.process_pending()
        finally:
            if self.durable:
                self.pending.close()

    def await_refinement(self, event):
        """等待同一 seq 的复核结果，超时或不可信时使用流式原文。"""
        raw = event['text'].strip()
        if event.get('refined_text'):
            return translation_source(raw, event['refined_text'])
        deadline = time.monotonic() + refinement_wait(event)
        with self.refined_ready:
            while event['seq'] not in self.refined and not self.closing.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.refined_ready.wait(remaining)
            refined = self.refined.pop(event['seq'], '')
        return translation_source(raw, refined)

    def process_pending(self):
        while not self.aborted and (not self.closing.is_set() or not self.pending.empty()):
            try:
                item = self.pending.get(timeout=.1)
            except queue.Empty:
                continue
            event = item['event'] if self.durable else item
            session_id = item['session_id'] if self.durable else self.session_id
            key = f'{session_id}:{event["seq"]}'
            translate_text = self.await_refinement(event) if self.refine else event['text'].strip()
            try:
                updates = caption_updates(event, event['seq'], session_id,
                                          self.llama_url, self.speech_url,
                                          translate_text=translate_text)
                next(updates)
                for record in updates:
                    if self.aborted:
                        break
                    changes = {name: value for name, value in record.items()
                               if name.startswith(('translation_', 'speech')) or name in
                               ('en', 'translate_ms', 'translated_at')}
                    changes['translation_source_text'] = translate_text
                    self.update(key, changes)
            except Exception as error:
                self.update(key, {'translation_status': 'failed', 'translation_error': str(error)})
            finally:
                if self.durable:
                    self.pending.task_done(retain=self.aborted)
                else:
                    self.pending.task_done()

    def close(self, timeout=None):
        self.closing.set()
        if timeout is None and not self.durable:
            timeout = 20
        self.worker.join(timeout=timeout)
        with self.lock:
            if self.worker.is_alive():
                for key, record in ([] if self.durable else list(self.latest.items())):
                    if record.get('translation_status') == 'pending':
                        self.update(key, {'translation_status': 'skipped',
                            'translation_error': 'Translation stopped at the shutdown deadline'})
                self.aborted = True
        return not self.worker.is_alive()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stt-model', required=True)
    parser.add_argument('--final-model', default=None, help='Optional SenseVoice final-utterance model directory')
    parser.add_argument('--utterance-dir', type=Path, help='Optional processed source-audio archive')
    parser.add_argument('--stt-port', type=int, default=18100)
    parser.add_argument('--llama-url', default='http://127.0.0.1:18200')
    parser.add_argument('--jsonl', default='/tmp/opencode/subtitles.jsonl')
    parser.add_argument('--speech-url', default='http://127.0.0.1:18080',
                        help='Speech-session API; speech is enabled in the control UI')
    parser.add_argument('--speak', action='store_true', help='Submit translated text to an enabled speech session')
    parser.add_argument('--delivery-policy', choices=['coverage', 'realtime'], default='coverage')
    parser.add_argument('--stable-clauses', action='store_true', help='Submit stable source clauses before the final recognition result')
    parser.add_argument('--boundary-model', type=Path, help='Optional local Chinese NNSplit ONNX model')
    parser.add_argument('--boundary-threshold', type=float, default=.5)
    parser.add_argument('--clause-max-chars', type=int, default=24)
    parser.add_argument('--queue-dir', type=Path, default=Path.home()/'.local/share/mozart/speech/captions')
    args = parser.parse_args()
    if args.boundary_model:
        args.stable_clauses = True
    if args.stable_clauses and args.delivery_policy != 'coverage':
        parser.error('stable-clauses requires the coverage delivery policy')
    if not 0 < args.boundary_threshold < 1 or not 12 <= args.clause_max_chars <= 64:
        parser.error('boundary-threshold must be between 0 and 1; clause-max-chars must be from 12 to 64')
    boundary_classifier = None
    if args.boundary_model:
        from boundary_classifier import BoundaryClassifier
        boundary_classifier = BoundaryClassifier(args.boundary_model, args.boundary_threshold, args.clause_max_chars)
    Path(args.jsonl).parent.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    stt_command = [sys.executable, str(Path(__file__).with_name('stt_service.py')),
        '--model', args.stt_model, '--port', str(args.stt_port), '--json']
    if args.final_model:
        stt_command += ['--final-model', args.final_model]
    if args.utterance_dir:
        stt_command += ['--utterance-dir',str(args.utterance_dir)]
    stt = subprocess.Popen(stt_command,
        stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
    def terminate(*_):
        # Leave stdout open and drain the child's final utterance before exiting.
        if stt.poll() is None:
            stt.terminate()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    print(f'[bridge] stt pid={stt.pid} port={args.stt_port}', file=sys.stderr, flush=True)
    try:
        with open(args.jsonl, 'a', encoding='utf-8') as captions:
            def publish(record):
                line = json.dumps(record, ensure_ascii=False)
                print(line, flush=True)
                captions.write(line+'\n')
                captions.flush()
            dispatcher_type = CaptionDispatcher
            if args.stable_clauses:
                from stable_clauses import StableClauseDispatcher
                dispatcher_type = StableClauseDispatcher
            dispatcher_options = {'refine': bool(args.final_model)}
            if boundary_classifier:
                dispatcher_options['boundary_classifier'] = boundary_classifier
            queue_name = ('translation-boundaries.sqlite3' if boundary_classifier else
                'translation-clauses.sqlite3' if args.stable_clauses else 'translation-queue.sqlite3')
            dispatcher = dispatcher_type(publish, session_id, args.llama_url,
                args.speech_url if args.speak else None,
                spool_path=(args.queue_dir/queue_name) if args.delivery_policy == 'coverage' else None,
                **dispatcher_options)
            try:
                for line in stt.stdout:
                    try:
                        event = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    dispatcher.accept(event)
            finally:
                dispatcher.close()
        stt.wait(timeout=5)
        if stt.returncode:
            raise RuntimeError(f'STT exited with status {stt.returncode}')
    finally:
        if stt.poll() is None:
            stt.terminate()
            try:
                stt.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stt.kill()
                stt.wait()
        stt.stdout.close()


if __name__ == '__main__':
    main()
