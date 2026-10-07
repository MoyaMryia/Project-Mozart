#!/usr/bin/env python3
"""Streaming Chinese ASR → English translation → captions and reference speech.

Captions are always published. Speech admission failures are explicit and never
fall back to reading Chinese through an English voice. The speech service owns
its bounded queue and playback device.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from translation_checks import missing_numbers, required_numbers, repeated_numbers, added_large_numbers, values_changed_to_item_counts, changed_loan_repayment, normalize_translation_quantities, MONTH_PATTERN, source_role_constraints, changed_explicit_roles
import re
from asr_checks import boundary_fragment_audit

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


def translate(url, text, timeout=10, audit=None):
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
    for attempt in range(2):
        result = request_json(url+'/v1/chat/completions', {
            'chat_template_kwargs': {'enable_thinking': False},
            'messages': messages, 'max_tokens': 300, 'temperature': 0}, timeout)
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
        if audit is not None:
            audit.append({'attempt':attempt+1,'content':content,'missing_numbers':missing,
                          'repeated_numbers':repeated,'added_large_numbers':added,
                          'values_changed_to_item_counts':value_counts,'loan_repayment_changed':repayment_changed,
                          'explicit_role_issues':role_issues, 'source_role_constraints':role_constraints,
                          'untranslated_chinese':untranslated,'finish_reason':choice.get('finish_reason'),
                          'translation_source':translation_source})
        if not missing and not repeated and not added and not value_counts and not repayment_changed and not role_issues and not untranslated and not truncated:
            return content, time.monotonic()-started
        # Start a fresh request: including the rejected answer in chat history
        # caused the small model to reproduce its hallucinated quantities.
        messages = [dict(messages[0]), dict(messages[1])]
        messages[0]['content'] += (
            ' Translate every source clause once. Do not repeat amounts or add explanations. '
            'Return a complete translation within the output limit.')
    reasons=[]
    if missing:reasons.append('required numerals missing: '+', '.join(missing))
    if repeated:reasons.append('unsupported repeated quantities: '+', '.join(repeated))
    if added:reasons.append('unsupported added amounts/years: '+', '.join(added))
    if value_counts:reasons.append('goods value changed to item count: '+', '.join(value_counts))
    if repayment_changed:reasons.append('explicit loan repayment lost or changed to borrowing')
    if role_issues:reasons.append('explicit source roles changed: '+', '.join(role_issues))
    if untranslated:reasons.append('untranslated Chinese in English output')
    if truncated:reasons.append('output token limit reached before completion')
    raise ValueError('Translation rejected: '+'; '.join(reasons))


def translate_recognized_event(url, event, audit=None):
    issues = [issue for key in ('asr_numeric_audit','asr_polarity_audit')
              for issue in event.get(key, {}).get('issues', [])]
    if issues:
        raise ValueError('Recognition uncertain: '+', '.join(issues))
    boundary = boundary_fragment_audit(event['text'].strip())
    if boundary is not None:
        raise ValueError('Source context uncertain: '+', '.join(boundary['issues']))
    return translate(url, event['text'].strip(), audit=audit)


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
    args = parser.parse_args()
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
            seq = 0
            for line in stt.stdout:
                try:
                    event = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if event.get('type') != 'final' or not event.get('text', '').strip():
                    continue
                seq += 1
                text = event['text'].strip()
                record = {'seq': seq, 'utterance_id': f'{session_id}:{event["seq"]}',
                    'zh': text, 'en': '', 'asr_engine': event.get('final_engine', 'zipformer'),
                    'received_at': time.time(),
                    'asr_emitted_at':event.get('emitted_at'),
                    'online_text': event.get('online_text', text), 'final_decode_ms': event.get('final_decode_ms', 0),
                    'ts': time.strftime('%H:%M:%S'), 'final': True}
                for name in ('source_audio','source_audio_sha256','source_audio_seconds','audio_start_pts_ns','audio_end_pts_ns','asr_numeric_audit','asr_polarity_audit'):
                    if name in event:record[name]=event[name]
                boundary = boundary_fragment_audit(text)
                if boundary is not None:
                    record['asr_boundary_audit'] = boundary
                translating = time.monotonic()
                try:
                    record['translation_audit'] = []
                    record['en'], elapsed = translate_recognized_event(args.llama_url, event, audit=record['translation_audit'])
                    record['translate_ms'] = int(elapsed*1000)
                except Exception as error:
                    record['translation_error'] = str(error)
                    if record.get('asr_numeric_audit', {}).get('issues'):
                        record['speech_skipped_reason'] = 'asr_numeric_uncertainty'
                    elif record.get('asr_polarity_audit', {}).get('issues'):
                        record['speech_skipped_reason'] = 'asr_polarity_uncertainty'
                    elif record.get('asr_boundary_audit', {}).get('issues'):
                        record['speech_skipped_reason'] = 'asr_boundary_uncertainty'
                    print(f'[bridge] translate failed: {error}', file=sys.stderr, flush=True)
                finally:
                    record['translate_ms'] = round((time.monotonic()-translating)*1000)
                    record['translated_at'] = time.time()
                if args.speak and record['en']:
                    try:
                        result = request_json(args.speech_url+'/api/speech/events', {
                            'text': record['en'], 'source_text': text,
                            'utterance_id': record['utterance_id']}, timeout=3)
                        record['speech'] = result.get('id', result.get('skipped', ''))
                    except Exception as error:
                        record['speech_error'] = str(error)
                print(json.dumps(record, ensure_ascii=False), flush=True)
                captions.write(json.dumps(record, ensure_ascii=False)+'\n')
                captions.flush()
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
