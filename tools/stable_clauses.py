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
from speech_outbox import SUBMISSION_PRIORITY


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

    def __init__(self, *args, boundary_classifier=None, **kwargs):
        self.boundary_worker = None
        self.ready = threading.Event()
        super().__init__(*args, **kwargs)
        if self.durable:
            with self.pending.lock, self.pending.database:
                self.pending.database.execute('CREATE TABLE IF NOT EXISTS clause_records (key TEXT PRIMARY KEY, payload TEXT)')
        self.ready.set()
        if boundary_classifier is not None:
            from boundary_classifier import BoundaryWorker
            self.boundary_worker = BoundaryWorker(boundary_classifier,
                lambda event, result: self.accept(event, _classified=result))

    def close(self, timeout=None):
        if self.boundary_worker is not None:
            self.boundary_worker.close()
        return super().close(timeout)

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

    def restore_record(self, key, index, snapshot):
        record = self.record(key)
        if record is None or index >= len(record.get('translation_segments', [])):
            recovered = deepcopy(snapshot)
            for position, part in enumerate((record or {}).get('translation_segments', [])):
                recovered['translation_segments'][position].update(part)
            self.update(key, recovered, create=True)
            record = self.record(key)
        if index >= len(record['translation_segments']):
            raise RuntimeError('Stored clause metadata is incomplete')
        return record

    def speech_update(self, context, changes):
        with self.lock:
            if self.aborted:
                return False
            key, index = context['key'], context['index']
            record = self.restore_record(key, index, context['record'])
            record['translation_segments'][index].update(changes)
            self.summary(record)
            self.update(key, record, create=True)
            context['record'] = deepcopy(self.latest[key])
            return True

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
        record['speech_error'] = '; '.join(dict.fromkeys(speech_errors))
        states = {p.get('speech_submission_status') for p in parts}
        for state in SUBMISSION_PRIORITY:
            if state in states:
                record['speech_submission_status'] = state
                break
        if any(p.get('speech_submission_status') for p in parts):
            record['speech_submission_attempts'] = sum(p.get('speech_submission_attempts', 0) for p in parts)
        record['speech_degraded'] = any(p.get('speech_degraded', False) for p in parts)

    def accept(self, event, _classified=None):
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
            if _classified is not None:
                from boundary_classifier import classifier_prefix
                if old is None:
                    return
                state = old['_clause_state']
                prefix = classifier_prefix(state['history'], state['committed'])
                if state['committed'] != _classified['committed'] or not prefix or not prefix.startswith(_classified['prefix']):
                    return
                record = deepcopy(old)
                event = {**event, 'text': record['zh'], 'emitted_at': record['asr_emitted_at']}
                record['boundary_classifier'] = {k: v for k, v in _classified.items()
                    if k not in ('prefix', 'committed')}
                record['boundary_classifier_warning'] = (
                    '断句模型失败；本段等待最终识别。' if _classified.get('error') else '')
            else:
                source = source_caption(event, event['seq'], self.session_id)
                record = {**(old or {}), **{k: v for k, v in source.items()
                    if k not in ('revision', 'en', 'translation_status')}}
            record['stable_clauses'] = True
            parts = record.setdefault('translation_segments', [])
            state = record.setdefault('_clause_state', {'committed': '', 'history': [], 'blocked': False})
            text = record['zh']
            at = event.get('emitted_at', time.time())
            if _classified is None:
                if state['history'] and at <= state['history'][-1][0]:
                    state['history'] = []
                state['history'] = (state['history']+[[at, text]])[-3:]
            if not text.startswith(state['committed']):
                state['blocked'] = True
                record['source_revision_warning'] = '识别改写了已提交前文；等待最终结果。'
            correction = record['final'] and not text.startswith(state['committed'])
            start = 0 if correction else len(state['committed'])
            end = len(text) if record['final'] else None
            early_limit = 8 if self.boundary_worker is not None else 2
            if not record['final'] and not state['blocked'] and len(parts) < early_limit:
                if self.boundary_worker is None:
                    end = stable_cut(state['history'], state['committed'])
                elif _classified is None:
                    from boundary_classifier import classifier_prefix
                    prefix = classifier_prefix(state['history'], state['committed'])
                    if prefix:
                        self.boundary_worker.submit(key, event, prefix, state['committed'])
                elif _classified['cut'] is not None:
                    end = _classified['cut']['end']
                    if not start < end <= len(_classified['prefix'])-4:
                        end = None
            if record['final']:
                record['source_revision_warning'] = ('已提交前文与最终识别不同；本次播报包含明确更正。' if correction else '')
            if end is not None and text[start:end].strip():
                index = len(parts)
                segment = {'index': index, 'source_start': start, 'source_end': end,
                    'source_text': text[start:end], 'submitted_at': time.time(),
                    'asr_emitted_at': event.get('emitted_at'), 'early': not record['final'],
                    'source_correction': correction, 'translation_status': 'pending', 'en': ''}
                if _classified is not None:
                    segment['boundary_classifier'] = deepcopy(record['boundary_classifier'])
                part_event = {**event, 'seq': f'{event["seq"]}/part/{index}', 'type': 'final',
                    'text': segment['source_text'], 'source_correction': correction,
                    'clause_parent_id': key, 'clause_index': index}
                # 当前识别前缀对应的观测区间包含前视音频，不是逐词强制对齐。
                # 各次提交只分配未使用的音频区间，避免重复使用父段总时长。
                from speech_duration import source_duration
                begin_pts = state.get('committed_audio_end_pts_ns', record.get('audio_start_pts_ns'))
                end_pts = record.get('audio_end_pts_ns')
                seconds = source_duration({'audio_start_pts_ns': begin_pts, 'audio_end_pts_ns': end_pts})
                part_event['source_duration_seconds'] = None if correction else seconds
                segment['source_duration_seconds'] = part_event['source_duration_seconds']
                segment['source_timing_basis'] = 'observed_interval_with_lookahead'
                parts.append(segment)
                previous_committed = state['committed']
                previous_audio_end = state.get('committed_audio_end_pts_ns')
                state['committed'] = text[:end]
                if seconds is not None and not correction:
                    state['committed_audio_end_pts_ns'] = end_pts
                item = {'event': part_event, 'session_id': self.session_id,
                    'queue_owner': self.queue_owner,
                    'work_key': f'{key}/part/{index}', 'record': deepcopy(record)}
                try:
                    self.pending.put_nowait(item)
                except Exception as error:
                    segment.update(translation_status='failed', translation_error=f'Translation task storage failed: {error}')
                    state['committed'] = previous_committed
                    if previous_audio_end is None:
                        state.pop('committed_audio_end_pts_ns', None)
                    else:
                        state['committed_audio_end_pts_ns'] = previous_audio_end
            if record['final']:
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
                if self.outbox is not None:
                    speech_key = f'{session_id}:{event["seq"]}'
                    restored = self.outbox.lookup(speech_key)
                    if restored is not None:
                        context, changes = restored
                        if self.speech_update(context, changes):
                            self.outbox.acknowledge(speech_key, changes, context)
                        continue
                with self.lock:
                    record = self.restore_record(key, index, item['record'])
                    parts = record['translation_segments']
                    previous = [p for p in parts[:index] if p.get('en') and p.get('translation_status') == 'completed']
                context = [{'source': p['source_text'][:256], 'translation': p['en'][:512]} for p in previous[-2:]]
                event['translation_context'] = context
                event['_translation_clock_current'] = item.get('queue_owner') == self.queue_owner
                updates = caption_updates(event, event['seq'], session_id, self.llama_url)
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
                        if self.outbox is not None and result['translation_status'] == 'completed':
                            context = {'key': key, 'index': index, 'record': deepcopy(self.latest[key])}
                            self.enqueue_speech(event, result, event['text'], context)
            except (OSError, ValueError) as error:
                with self.lock:
                    record = self.record(key)
                    if record and index < len(record.get('translation_segments', [])):
                        record['translation_segments'][index].update(translation_status='failed', translation_error=str(error))
                        self.summary(record)
                        self.update(key, record)
            except Exception:
                self.aborted = True
                raise
            finally:
                if self.durable:
                    self.pending.task_done(retain=self.aborted)
                else:
                    self.pending.task_done()
