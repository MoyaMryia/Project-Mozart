"""Submit stable source prefixes. Preserve the final source and segment history."""
from copy import deepcopy
import json
import os
import queue
import re
import threading
import time

from asr_checks import boundary_fragment_audit
from subtitle_bridge import CaptionDispatcher, caption_updates, source_caption


BOUNDARY = re.compile(r'[，。！？；,!?;]|然后|但是|不过|反正|接着|完了')
DEPENDENT = re.compile(r'如果|假如|虽然|只要|除非|因为|无论|不管')
OPEN_END = re.compile(
    r'(?:的|不|没|没有|要|在|和|与|把|被|将|跟|给|从|比|是|叫|说|觉得|认为|'
    r'心想|发现|决定|价值|等于|大于|少于|超过|至少|或者|为了|由于|'
    r'[0-9零〇一二两三四五六七八九十百千万亿点])$')


def stable_cut(history, committed):
    """Find a conservative boundary after three unchanged prefix observations."""
    if len(history) < 3 or history[-1][0]-history[0][0] < .6:
        return None
    prefix = os.path.commonprefix([item[1] for item in history])
    if not prefix.startswith(committed):
        return None
    start = len(committed)
    for match in BOUNDARY.finditer(prefix, start):
        punctuation = len(match.group()) == 1
        end = match.end() if punctuation else match.start()
        source = prefix[start:end].strip('，。！？；,!?; ')
        if len(source) < 10 or len(prefix)-end < 4:
            continue
        if DEPENDENT.search(source) or OPEN_END.search(source):
            continue
        if any(source.count(left) != source.count(right) for left, right in [('“', '”'), ('（', '）'), ('(', ')')]):
            continue
        if boundary_fragment_audit(source):
            continue
        return end
    return None


class StableClauseDispatcher(CaptionDispatcher):
    """Keep one caption row and ordered, durable translation tasks per source."""

    def __init__(self, *args, **kwargs):
        self.ready = threading.Event()
        super().__init__(*args, **kwargs)
        if self.durable:
            with self.pending.lock, self.pending.database:
                self.pending.database.execute('CREATE TABLE IF NOT EXISTS clause_records (key TEXT PRIMARY KEY, payload TEXT)')
        self.ready.set()

    def run(self):
        self.ready.wait()
        super().run()

    def record(self, key):
        if key in self.latest:
            return deepcopy(self.latest[key])
        if self.durable:
            with self.pending.lock:
                row = self.pending.database.execute('SELECT payload FROM clause_records WHERE key=?', (key,)).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def update(self, key, changes, create=False):
        with self.lock:
            previous = self.record(key)
            if self.aborted or (previous is None and not create):
                return
            record = {**(previous or {}), **deepcopy(changes), 'revision': (previous or {}).get('revision', -1)+1}
            if self.durable:
                with self.pending.lock, self.pending.database:
                    self.pending.database.execute('INSERT OR REPLACE INTO clause_records VALUES (?,?)',
                        (key, json.dumps(record, ensure_ascii=False)))
            self.latest[key] = record
            self.latest.move_to_end(key)
            while len(self.latest) > 128:
                self.latest.popitem(last=False)
            self.publish({k: deepcopy(v) for k, v in record.items() if not k.startswith('_')})

    @staticmethod
    def summary(record):
        parts = record.get('translation_segments', [])
        corrections = [p for p in parts if p.get('source_correction')]
        visible = corrections[-1:] if corrections and corrections[-1].get('en') else parts
        record['en'] = ' '.join(p.get('en', '') for p in visible if p.get('en'))
        errors = [p['translation_error'] for p in parts if p.get('translation_error')]
        if errors:
            record['translation_error'] = '; '.join(dict.fromkeys(errors))
        if any(p.get('translation_status') == 'failed' for p in parts):
            record['translation_status'] = 'failed'
        elif any(p.get('translation_status') == 'pending' for p in parts):
            record['translation_status'] = 'pending'
        else:
            record['translation_status'] = 'completed' if record.get('final') else 'recognizing'
        jobs = [p['speech'] for p in parts if p.get('speech')]
        record['speech_jobs'] = jobs
        if jobs:
            record['speech'] = jobs[-1]
        speech_errors = [p['speech_error'] for p in parts if p.get('speech_error')]
        if speech_errors:
            record['speech_error'] = '; '.join(dict.fromkeys(speech_errors))
        record['speech_degraded'] = any(p.get('speech_degraded', False) for p in parts)

    def accept(self, event):
        if event.get('type') == 'refined':
            return super().accept(event)
        if self.closing.is_set() or event.get('type') not in ('partial', 'final') or not event.get('text', '').strip():
            return
        key = f'{self.session_id}:{event["seq"]}'
        with self.lock:
            if self.closing.is_set():
                return
            old = self.record(key)
            if old and old.get('final'):
                return
            source = source_caption(event, event['seq'], self.session_id)
            record = {**(old or {}), **{k: v for k, v in source.items()
                if k not in ('revision', 'en', 'translation_status')}}
            record['stable_clauses'] = True
            parts = record.setdefault('translation_segments', [])
            state = record.setdefault('_clause_state', {'committed': '', 'history': [], 'blocked': False})
            text = source['zh']
            at = event.get('emitted_at', time.time())
            if state['history'] and at <= state['history'][-1][0]:
                state['history'] = []
            state['history'] = (state['history']+[[at, text]])[-3:]
            if not text.startswith(state['committed']):
                state['blocked'] = True
                record['source_revision_warning'] = '识别改写了已提交前文；等待最终结果。'
            correction = source['final'] and not text.startswith(state['committed'])
            start = 0 if correction else len(state['committed'])
            end = len(text) if source['final'] else None
            if not source['final'] and not state['blocked'] and len(parts) < 2:
                end = stable_cut(state['history'], state['committed'])
            if source['final']:
                record['source_revision_warning'] = ('已提交前文与最终识别不同；本次播报包含明确更正。' if correction else '')
            if end is not None and text[start:end].strip():
                index = len(parts)
                segment = {'index': index, 'source_start': start, 'source_end': end,
                    'source_text': text[start:end], 'submitted_at': time.time(),
                    'asr_emitted_at': event.get('emitted_at'), 'early': not source['final'],
                    'source_correction': correction, 'translation_status': 'pending', 'en': ''}
                part_event = {**event, 'seq': f'{event["seq"]}/part/{index}', 'type': 'final',
                    'text': segment['source_text'], 'source_correction': correction,
                    'clause_parent_id': key, 'clause_index': index}
                parts.append(segment)
                previous_committed = state['committed']
                state['committed'] = text[:end]
                item = {'event': part_event, 'session_id': self.session_id,
                    'work_key': f'{key}/part/{index}', 'record': deepcopy(record)}
                try:
                    self.pending.put_nowait(item)
                except Exception as error:
                    segment.update(translation_status='failed', translation_error=f'Translation task storage failed: {error}')
                    state['committed'] = previous_committed
            if source['final']:
                state['history'] = []
            self.summary(record)
            self.update(key, record, create=True)

    def process_pending(self):
        while not self.aborted and (not self.closing.is_set() or not self.pending.empty()):
            try:
                item = self.pending.get(timeout=.1)
            except queue.Empty:
                continue
            event, session_id = item['event'], item['session_id']
            key, index = event['clause_parent_id'], event['clause_index']
            try:
                with self.lock:
                    record = self.record(key)
                    if record is None or index >= len(record.get('translation_segments', [])):
                        # Recover the source snapshot if storage stopped between two writes.
                        recovered = deepcopy(item['record'])
                        for position, part in enumerate((record or {}).get('translation_segments', [])):
                            recovered['translation_segments'][position].update(part)
                        self.update(key, recovered, create=True)
                        record = self.record(key)
                    parts = record['translation_segments']
                    if index >= len(parts):
                        raise RuntimeError('Stored clause metadata is incomplete')
                    previous = [p for p in parts[:index] if p.get('en') and p.get('translation_status') == 'completed']
                context = [{'source': p['source_text'][:256], 'translation': p['en'][:512]} for p in previous[-2:]]
                event['translation_context'] = context
                updates = caption_updates(event, event['seq'], session_id, self.llama_url, self.speech_url)
                next(updates)
                for result in updates:
                    with self.lock:
                        if self.aborted:
                            break
                        record = self.record(key)
                        part = record['translation_segments'][index]
                        part.update({k: v for k, v in result.items() if k.startswith(('translation_', 'speech'))
                            or k in ('en', 'translate_ms', 'translated_at')})
                        self.summary(record)
                        self.update(key, record)
            except Exception as error:
                with self.lock:
                    record = self.record(key)
                    if record and index < len(record.get('translation_segments', [])):
                        record['translation_segments'][index].update(translation_status='failed', translation_error=str(error))
                        self.summary(record)
                        self.update(key, record)
            finally:
                if self.durable:
                    self.pending.task_done(retain=self.aborted)
                else:
                    self.pending.task_done()
