#!/usr/bin/env python3
"""Reference voice registry, disk-backed speech jobs, and one owned ALSA output.

Runs on loopback behind Mozart's HTTP API. Inference is isolated in a warm child
process so cancellation/timeouts can release the engine without stopping captions.
"""
import argparse
import base64
from collections import OrderedDict, deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit
import uuid
import wave
from speech_chunks import estimated_audio_seconds, split_speech_text
from durable_jobs import DiskJobs, DiskPending
from tempo_player import TempoPlayer, tempo_library
from speech_duration import DurationEstimator, DEFAULT_PROFILE
from tempo_control import DEFAULT_TEMPO_MAX, TempoController

TERMINAL = {'completed', 'failed', 'cancelled', 'expired'}


class WorkerError(RuntimeError):
    pass


class TaskRejected(WorkerError):
    pass


class ApiError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def atomic_json(path, value):
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


class SpeechService:
    def __init__(self, root, worker_command, engine='pocket', playback_device='default',
                 queue_limit=4, timeout=90, history_limit=128, preload=False,
                 delivery_policy='realtime', playback_tempo='normal', capture_playback=False,
                 duration_profile=DEFAULT_PROFILE, playback_tempo_max=DEFAULT_TEMPO_MAX,
                 playback_target_delay=1.5, playback_recovery_seconds=30.):
        if playback_tempo not in ('normal', 'adaptive', 'flexible'):
            raise ValueError('Unknown playback tempo policy')
        self.playback_tempo, self.capture_playback = playback_tempo, capture_playback
        self.duration_estimator = DurationEstimator(duration_profile)
        self.tempo_controller = TempoController(playback_tempo_max, playback_target_delay, playback_recovery_seconds)
        if playback_tempo in ('adaptive', 'flexible'):
            tempo_library()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'voices').mkdir(exist_ok=True)
        (self.root/'results').mkdir(exist_ok=True)
        self.command, self.engine, self.device = worker_command, engine, playback_device
        self.queue_limit, self.timeout, self.history_limit = queue_limit, timeout, history_limit
        self.lock = threading.Condition(threading.RLock())
        self.delivery_policy = delivery_policy
        self.storage_warning = None
        self.storage_failed = False
        self.discard_warning = None
        self.voices = {p.stem: json.loads(p.read_text()) for p in (self.root/'voices').glob('*.json')}
        self.jobs, self.pending, self.keys = OrderedDict(), deque(), OrderedDict()
        if self.delivery_policy == 'coverage':
            self.jobs = DiskJobs(self.root/'speech-jobs.sqlite3', history_limit,
                pinned=lambda: {getattr(self, 'active', None), getattr(self, 'active_play', None)}-{None})
            self.pending = DiskPending(self.jobs)
        self.play_pending = deque()
        self.playback_limit_seconds = 12
        self.live_audio_budget_seconds = 24
        self.deferred_limit = 4
        self.active_play_piece = None
        paths = [] if isinstance(self.jobs, DiskJobs) and len(self.jobs) else sorted((self.root/'results').glob('*.json'), key=lambda p: p.stat().st_mtime)
        for path in paths:
            job = json.loads(path.read_text())
            if job['status'] not in TERMINAL and self.delivery_policy != 'coverage':
                job.update(status='failed', error='Speech service restarted before completion')
                atomic_json(path, job)
            self.jobs[job['id']] = job
            if job.get('utterance_id') and self.delivery_policy != 'coverage':
                self.keys[job['utterance_id']] = job['id']
        if self.delivery_policy == 'coverage':
            for key in self.jobs.ids("status NOT IN ('completed','failed','cancelled','expired')"):
                job = self.jobs[key]
                if 'playback_started_at' in job:
                    job.update(status='failed', error='Playback was interrupted; automatic replay is disabled')
                    self.save_job(job)
                else:
                    self.reset_waiting(job)
        self.prune()
        self.session = {'enabled': False, 'voice_id': '', 'language': 'en', 'playback': False}
        self.child = self.player = None
        self.player_job_id = None
        self.output_buffer = b''
        self.closed = False
        self.runtime = self.last_runtime = None
        self.runtime_state = 'unprepared'
        self.runtime_error = self.worker_error = self.last_runtime_error = None
        self.last_preparation = None
        self.runtime_epoch = 0
        self.prepare_requested = preload
        self.preparation = self.new_preparation() if preload else None
        if preload:
            self.runtime_state = 'queued'
        self.preload = preload
        self.warmup_error = None
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.play_worker = threading.Thread(target=self.play_loop, daemon=True)
        self.worker.start()
        self.play_worker.start()

    def status(self):
        with self.lock:
            coverage = self.delivery_policy == 'coverage'
            jobs = self.jobs.recent(self.history_limit) if coverage else [dict(j) for j in reversed(self.jobs.values())]
            runtime = self.runtime_status()
            return {'available': True, 'engine': self.engine, **runtime,
                'delivery_policy': self.delivery_policy,
                'storage_warning': self.storage_warning,
                'storage_failed': self.storage_failed,
                'unfinished_jobs': self.jobs.unfinished() if coverage else sum(j['status'] not in TERMINAL for j in self.jobs.values()),
                'pending_jobs': len(self.pending),
                'resident_jobs': len(self.jobs.cache) if coverage else len(self.jobs),
                'languages': ['en'] if self.engine == 'pocket' else ['en', 'zh'],
                'reference_tts': True, 'streaming_playback': bool(runtime['runtime_ready'] and self.runtime.get('audio_stream_protocol') == 1),
                'chunked_playback': True, 'chunk_target_words': 12,
                'live_audio_budget_seconds': self.live_audio_budget_seconds,
                'audio_backlog_seconds': self.audio_backlog_seconds(),
                'buffered_audio_seconds': self.buffered_audio_seconds(),
                'deferred_jobs': 0 if coverage else sum(j.get('admission_pending', False) and j['status'] not in TERMINAL for j in self.jobs.values()),
                'session': dict(self.session), 'queue_limit': self.queue_limit,
                'playback_device': self.device, 'playback_queue_limit_seconds': self.playback_limit_seconds,
                'playback_tempo_policy': self.playback_tempo,
                'playback_tempo_max': self.tempo_controller.maximum,
                'duration_profile': (self.duration_estimator.profile or {}).get('id'),
                'duration_profile_warning': self.duration_estimator.warning,
                'voices': list(self.voices.values()),
                'jobs': jobs}

    def new_preparation(self):
        previous = getattr(self, 'preparation', None)
        if previous:
            self.last_preparation = dict(previous)
        self.runtime_error = self.warmup_error = None
        now = time.time()
        self.prepare_deadline = time.monotonic()+self.timeout
        return {'id': uuid.uuid4().hex, 'status': 'queued',
                'requested_at': now, 'deadline_at': now+self.timeout}

    def invalidate_runtime(self, reason, state):
        self.runtime = None
        self.runtime_state, self.runtime_error = state, reason
        if state == 'failed':
            self.last_runtime_error = {'message': reason, 'at': time.time(),
                                       'pid': self.child.pid if self.child else None,
                                       'exit_code': self.child.poll() if self.child else None}
        self.runtime_epoch += 1
        self.prepare_requested = False
        if self.preparation and self.preparation['status'] in ('queued', 'preparing'):
            self.preparation.update(status='cancelled' if state in ('stopped', 'closed') else 'failed',
                                    error=reason, finished_at=time.time())

    def refresh_runtime(self):
        if (self.preparation and self.preparation['status'] == 'queued'
                and time.monotonic() >= self.prepare_deadline):
            self.invalidate_runtime('Speech preparation timed out before startup', 'failed')
            self.warmup_error = self.runtime_error
        if self.child and self.child.poll() is not None and self.runtime_state in ('ready', 'preparing'):
            preparing = self.runtime_state == 'preparing'
            self.invalidate_runtime(f'Speech worker exited with code {self.child.returncode}', 'failed')
            if preparing:
                self.warmup_error = self.runtime_error

    def runtime_status(self):
        self.refresh_runtime()
        ready = bool(self.runtime is not None and self.child and self.child.poll() is None
                     and self.worker.is_alive() and not self.closed and self.worker_error is None)
        return {'runtime_ready': ready, 'runtime': dict(self.runtime) if ready else None,
                'last_runtime': dict(self.last_runtime) if self.last_runtime else None,
                'runtime_state': self.runtime_state, 'runtime_error': self.runtime_error,
                'warmup_error': self.warmup_error,
                'last_runtime_error': dict(self.last_runtime_error) if self.last_runtime_error else None,
                'last_prepare': dict(self.last_preparation) if self.last_preparation else None,
                'worker_error': dict(self.worker_error) if self.worker_error else None,
                'worker_alive': self.worker.is_alive(),
                'playback_worker_alive': self.play_worker.is_alive(),
                'runtime_pid': self.child.pid if self.child else None,
                'runtime_exit_code': self.child.poll() if self.child else None,
                'prepare': dict(self.preparation) if self.preparation else None}

    def prepare(self):
        with self.lock:
            current = self.runtime_status()
            if self.closed or self.worker_error or self.storage_failed or not self.worker.is_alive():
                raise ApiError(503, 'Speech worker is unavailable; restart the service')
            if current['runtime_ready'] or (self.preparation and
                    self.preparation['status'] in ('queued', 'preparing')):
                return current
            self.preparation = self.new_preparation()
            self.prepare_requested = True
            self.runtime_state = 'queued'
            self.lock.notify_all()
            return self.runtime_status()

    def sync_playback_start(self, job):
        if (getattr(self, 'active_play', None) == job['id'] and
                getattr(self, 'player_job_id', None) == job['id'] and
                isinstance(self.player, TempoPlayer) and self.player.first_audio_at):
            job.setdefault('playback_started_at', self.player.first_audio_at)
            job['playback_timing'] = 'null_paced_filtered_pcm' if self.device == 'null' else 'filtered_pcm_pipe_delivery'

    def save_job(self, job):
        self.sync_playback_start(job)
        if self.storage_failed:
            return
        if self.delivery_policy == 'coverage':
            try:
                self.jobs[job['id']] = job
            except sqlite3.Error as error:
                self.note_storage_failure(error)
                raise
            try:
                atomic_json(self.root/'results'/f"{job['id']}.json", job)
            except OSError as error:
                self.note_storage_warning(error, 'JSON snapshot failed; the SQLite task remains stored')
            return
        try:
            atomic_json(self.root/'results'/f"{job['id']}.json", job)
        except OSError as error:
            self.note_storage_failure(error)
            raise

    def note_storage_warning(self, error, message):
        """记录可查询的存储降级原因。服务继续接收任务。"""
        with self.lock:
            self.storage_warning = str(error)
            self.lock.notify_all()
        print(f'[speech] {message}: {error}', file=sys.stderr, flush=True)

    def note_storage_failure(self, error):
        """记录不可继续的存储故障：停止接收新任务。"""
        self.note_storage_warning(error, 'task storage failed; the service stops new tasks')
        with self.lock:
            self.storage_failed = True
            self.lock.notify_all()

    def owned_job(self, job_id):
        """Read a resident job. Do not reload records evicted from the disk cache."""
        return self.jobs.cache.get(job_id) if self.delivery_policy == 'coverage' else self.jobs.get(job_id)

    def fatal_error(self, error, component, jobs=()):
        """Stop the service in memory. Fail the owned jobs and keep one reason."""
        with self.lock:
            if isinstance(error, sqlite3.Error):
                self.note_storage_failure(error)
            if self.worker_error is None:
                self.worker_error = {'type': type(error).__name__, 'message': str(error),
                                     'at': time.time(), 'component': component}
            self.invalidate_runtime(f'Internal speech {component} error: {error}', 'failed')
            self.session['enabled'] = False
            self.kill(self.child)
            self.kill(self.player)
            failed = {job['id']: job for job in jobs if job is not None}
            for key in {getattr(self, 'active', None), getattr(self, 'active_play', None)}-{None}:
                job = self.owned_job(key)
                if job is not None:
                    failed.setdefault(key, job)
            # 已生成但仍等待播放的任务同样无法继续。
            for piece in self.play_pending:
                job = self.owned_job(piece['job_id'])
                if job is not None:
                    failed.setdefault(job['id'], job)
            self.play_pending = deque()
            # realtime 的等待队列在内存中，就地失败。coverage 的磁盘等待队列
            # 保留 pending 状态，供重启后恢复，不整体读入内存。
            if self.delivery_policy != 'coverage':
                for job_id in self.pending:
                    job = self.owned_job(job_id)
                    if job is not None:
                        failed.setdefault(job_id, job)
                self.pending.clear()
            self.active_play = self.active_play_piece = None
            self.player_job_id = None
            finished_at = time.time()
            for job in failed.values():
                if job['status'] not in TERMINAL:
                    job.update(status='failed', error=self.runtime_error, finished_at=finished_at)
                else:
                    job.setdefault('finished_at', finished_at)
                if self.storage_failed:
                    continue
                try:
                    self.save_job(job)
                except (OSError, sqlite3.Error) as persist_error:
                    self.note_storage_failure(persist_error)
            self.lock.notify_all()

    def reset_waiting(self, job):
        for name in ('error', 'finished_at', 'started_at', 'queue_seconds', 'admitted_at',
                     'audio_ready_at', 'generation_finished_at', 'duration_seconds',
                     'synthesis_seconds', 'sample_rate', 'first_callback_seconds',
                     'first_text_part_synthesis_seconds', 'expired_stage'):
            job.pop(name, None)
        job.update(status='queued', chunks=[], generated_chunks=0, played_chunks=0,
                   generation_done=False, played_duration_seconds=0, admission_pending=False,
                   text_part_results=[])
        self.save_job(job)
        self.pending.append(job['id'])

    def add_voice(self, body):
        import numpy as np
        import soundfile as sf
        name = str(body.get('name', '')).strip()
        if not name or len(name) > 80:
            raise ApiError(400, 'Reference name must contain 1–80 characters')
        try:
            raw = base64.b64decode(body['audio_base64'], validate=True)
            if len(raw) > 5*1024**2:
                raise ValueError('Reference exceeds 5 MiB')
            with sf.SoundFile(io.BytesIO(raw)) as audio:
                if audio.format != 'WAV' or not 8000 <= audio.samplerate <= 48000:
                    raise ValueError('Upload a WAV at 8–48 kHz')
                duration = len(audio)/audio.samplerate
                if not 3 <= duration <= 15 or audio.channels > 2:
                    raise ValueError('Reference must be 3–15 seconds, mono or stereo')
                rate = audio.samplerate
                pcm = audio.read(dtype='float32', always_2d=True).mean(axis=1)
            if not np.isfinite(pcm).all() or np.max(np.abs(pcm)) > 1 or np.sqrt(np.mean(pcm**2)) < .0001:
                raise ValueError('Reference is silent, non-finite or clips')
        except (KeyError, ValueError, RuntimeError) as error:
            raise ApiError(400, str(error)) from error
        transcript = str(body.get('transcript', '')).strip()
        if len(transcript) > 1000:
            raise ApiError(400, 'Reference transcript exceeds 1000 characters')
        if self.engine == 'zipvoice' and not transcript:
            raise ApiError(400, 'ZipVoice requires the exact reference transcript')
        with self.lock:
            if len(self.voices) >= 32:
                raise ApiError(409, 'Voice registry is full (32 profiles)')
            voice_id = uuid.uuid4().hex
            path = self.root/'voices'/f'{voice_id}.wav'
            sf.write(path, pcm, rate, subtype='PCM_16')
            voice = {'id': voice_id, 'name': name, 'transcript': transcript,
                'reference_language': str(body.get('language', 'zh')),
                'revision': hashlib.sha256(path.read_bytes()).hexdigest(),
                'duration_seconds': duration, 'engine': self.engine, 'status': 'needs_preview',
                'identity_approved': False}
            atomic_json(path.with_suffix('.json'), voice)
            self.voices[voice_id] = voice
            return dict(voice)

    def delete_voice(self, voice_id):
        with self.lock:
            if voice_id not in self.voices:
                raise ApiError(404, 'Reference voice not found')
            busy = self.jobs.voice_busy(voice_id) if self.delivery_policy == 'coverage' else any(j['voice_id'] == voice_id and j['status'] not in TERMINAL for j in self.jobs.values())
            if busy:
                raise ApiError(409, 'Reference is in use by an unfinished job')
            if self.session['voice_id'] == voice_id:
                self.session.update(enabled=False, voice_id='')
            for suffix in ['.wav', '.json']:
                (self.root/'voices'/f'{voice_id}{suffix}').unlink(missing_ok=True)
            del self.voices[voice_id]
            return {'deleted': voice_id}

    def submit(self, body):
        text = str(body.get('text', '')).strip()
        language = body.get('language', 'en')
        degraded = body.get('speech_degraded', False)
        timing = {}
        for name, maximum in [('source_duration_seconds', 300), ('source_emitted_at', time.time()+5)]:
            value = body.get(name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum or not math.isfinite(value):
                    raise ApiError(400, f'{name} must be a finite positive number within its range')
                timing[name] = value
        if not isinstance(degraded, bool):
            raise ApiError(400, 'speech_degraded must be a boolean')
        if degraded:
            raise ApiError(422, 'Rejected translations cannot enter speech; inspect the caption error')
        if language not in (['en'] if self.engine == 'pocket' else ['en', 'zh']):
            raise ApiError(422, 'Selected engine does not support this output language')
        if not text or len(text) > 500:
            raise ApiError(400, 'Speech text must contain 1–500 characters')
        if language == 'en' and any('\u3400' <= c <= '\u9fff' for c in text):
            raise ApiError(422, 'English output contains Chinese text; translation is required')
        with self.lock:
            if self.closed or self.worker_error or self.storage_failed:
                raise ApiError(503, 'Speech worker is unavailable; restart the service')
            key = str(body.get('utterance_id', ''))
            if len(key) > 128:
                raise ApiError(400, 'Utterance ID exceeds 128 characters')
            existing = self.jobs.find_utterance(key) if key and self.delivery_policy == 'coverage' else self.keys.get(key)
            if existing and existing in self.jobs:
                return dict(self.jobs[existing])
            voice = self.voices.get(body.get('voice_id'))
            if voice is None:
                raise ApiError(404, 'Reference voice not found')
            if voice['engine'] != self.engine:
                raise ApiError(422, 'Reference profile belongs to a different engine')
            live = body.get('live', False)
            playback = body.get('playback', False)
            if not isinstance(live, bool) or not isinstance(playback, bool):
                raise ApiError(400, 'live and playback must be booleans')
            texts = split_speech_text(text, language) if live or playback else [text]
            estimate = sum(estimated_audio_seconds(piece, language) for piece in texts)
            prediction = None
            if self.playback_tempo == 'flexible':
                prediction = self.duration_estimator.predict(texts, language, voice['revision'], self.runtime)
                # 容量限制保留原有保守估计；学习模型只增加必要的误差余量。
                estimate = max(estimate, prediction['reserve_seconds'])
            unfinished = [] if self.delivery_policy == 'coverage' else [j for j in self.jobs.values() if j['status'] not in TERMINAL]
            admission_pending = False
            if self.delivery_policy == 'coverage':
                pass
            elif live and playback:
                deferred = sum(j.get('admission_pending', False) for j in unfinished)
                if len(unfinished) >= 64 or estimate > self.live_audio_budget_seconds:
                    raise ApiError(429, 'Estimated speech delay exceeds the live audio budget; captions continue')
                # Include deferred text when predicting FIFO playback, unlike
                # the actual audio-reservation budget. A bounded queue must not
                # knowingly accept work that cannot start before its deadline.
                first_piece = estimated_audio_seconds(texts[0], language)
                predicted_first_play = self.audio_backlog_seconds(include_deferred=True)+first_piece
                if predicted_first_play >= 30:
                    raise ApiError(429, 'Estimated first playback exceeds the live deadline; captions continue')
                if deferred or self.audio_backlog_seconds()+estimate > self.live_audio_budget_seconds:
                    if deferred >= self.deferred_limit:
                        raise ApiError(429, 'Deferred speech queue is full; captions continue')
                    admission_pending = True
            elif len(self.pending)+int(getattr(self, 'active', None) is not None) >= self.queue_limit:
                raise ApiError(429, 'Synthesis queue is full; captions continue')
            job_id = uuid.uuid4().hex
            job = {'id': job_id, 'text': text, 'language': language,
                'voice_id': voice['id'], 'voice_revision': voice['revision'],
                'reference_text': voice['transcript'], 'engine': self.engine,
                'utterance_id': key, 'source_text': str(body.get('source_text', ''))[:2000],
                'status': 'queued', 'created_at': time.time(),
                'live': live, 'playback': playback, 'speech_degraded': degraded,
                'admission_pending': admission_pending,
                'was_deferred': admission_pending, 'admission_wait_seconds': 0,
                'chunk_texts': texts, 'chunk_count': len(texts), 'chunks': [],
                'generated_chunks': 0, 'played_chunks': 0, 'generation_done': False,
                'estimated_duration_seconds': estimate, 'played_duration_seconds': 0,
                'max_duration_seconds': min(45, max(4, (len(text)*.35 if language == 'zh' else len(text.split())*.9)+2)),
                'result_url': f'/api/speech/jobs/{job_id}/result', **timing}
            if prediction:
                job['duration_prediction'] = prediction
            try:
                self.jobs[job_id] = job
                self.save_job(job)
                self.pending.append(job_id)
            except (OSError, sqlite3.Error) as error:
                self.note_storage_failure(error)
                self.fatal_error(error, 'submit', [job])
                raise ApiError(507, 'Speech task storage failed; inspect the service log') from error
            if key and self.delivery_policy != 'coverage':
                self.keys[key] = job_id
                while len(self.keys) > 256:
                    self.keys.popitem(last=False)
            self.prune()
            self.lock.notify_all()
            return dict(job)

    def audio_backlog_seconds(self, include_deferred=False):
        now = time.time()
        coverage = self.delivery_policy == 'coverage'
        total = self.pending.estimate() if coverage else 0
        jobs = (self.jobs[key] for key in self.jobs.ids("pending=0 AND status NOT IN ('completed','failed','cancelled','expired')")) if coverage else self.jobs.values()
        for job in jobs:
            if job['status'] in TERMINAL or not job['playback'] or (job.get('admission_pending') and not include_deferred):
                continue
            duration = job.get('duration_seconds', 0)
            if not job.get('generation_done'):
                duration = max(duration, job.get('estimated_duration_seconds', 0))
            remaining = duration-job.get('played_duration_seconds', 0)
            if self.active_play_piece and self.active_play_piece['job_id'] == job['id']:
                remaining -= min(self.active_play_piece['duration_seconds'],
                                 max(0, now-self.active_play_piece['playback_started_at'])*job.get('playback_tempo', 1))
            total += max(0, remaining)
        return total

    def buffered_audio_seconds(self):
        total = sum(p['duration_seconds'] for p in self.play_pending
                    if self.jobs[p['job_id']]['status'] not in TERMINAL)
        if self.active_play_piece:
            total += max(0, self.active_play_piece['duration_seconds']-
                         (time.time()-self.active_play_piece['playback_started_at'])*
                         self.jobs[self.active_play_piece['job_id']].get('playback_tempo', 1))
        return total

    def prune(self):
        if self.storage_failed:
            return
        coverage = self.delivery_policy == 'coverage'
        candidates = self.jobs.history_to_remove(self.history_limit) if coverage else list(self.jobs)
        for job_id in candidates:
            if not coverage and len(self.jobs) <= self.history_limit:
                break
            if self.jobs[job_id]['status'] in TERMINAL and job_id not in {getattr(self, 'active', None), getattr(self, 'active_play', None)}:
                # 文件删不掉时保留任务记录，下次清理重试。
                if not self.discard_results(job_id):
                    continue
                del self.jobs[job_id]

    def discard_results(self, job_id):
        """删除任务的结果文件。删除失败时记录原因并返回 False。"""
        try:
            paths = [self.root/'results'/f'{job_id}.wav', self.root/'results'/f'{job_id}.json',
                     self.root/'results'/f'{job_id}-playback.wav']
            paths.extend((self.root/'results').glob(f'{job_id}-part-*.wav'))
            for path in paths:
                path.unlink(missing_ok=True)
        except OSError as error:
            if self.discard_warning != str(error):
                self.discard_warning = str(error)
                self.note_storage_warning(error, 'result file removal failed; the task record is kept')
            return False
        self.discard_warning = None
        return True

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ApiError(404, 'Speech job not found')
            if job['status'] not in TERMINAL:
                job['status'] = 'cancelled'
                if self.delivery_policy == 'coverage':
                    if not self.storage_failed:
                        try:
                            self.pending.discard(job_id)
                        except sqlite3.Error as error:
                            self.note_storage_failure(error)
                            self.fatal_error(error, 'cancel', [job])
                            if not self.closed:
                                raise ApiError(507, 'Speech task storage failed; inspect the service log') from error
                else:
                    self.pending = deque(i for i in self.pending if i != job_id)
                self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job_id)
                if getattr(self, 'active', None) == job_id:
                    if not self.closed:
                        self.invalidate_runtime('Active speech task cancelled', 'stopped')
                    self.kill(self.child)
                if getattr(self, 'active_play', None) == job_id:
                    self.kill(self.player)
            self.save_job(job)
            self.lock.notify_all()
            return dict(job)

    @staticmethod
    def kill(process):
        if process and (isinstance(process, TempoPlayer) or process.poll() is None):
            # 先关闭输入，解除 aplay 对原始音频管道的阻塞读取。
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            process.terminate()
            try:
                process.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def read_child(self, timeout=None):
        end = time.monotonic()+min(self.timeout, timeout if timeout is not None else self.timeout)
        while time.monotonic() < end and not self.closed:
            while b'\n' in self.output_buffer:
                line, self.output_buffer = self.output_buffer.split(b'\n', 1)
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        return value
                except (ValueError, UnicodeDecodeError):
                    continue
            if self.child.poll() is not None:
                raise WorkerError(f'Speech worker exited with code {self.child.returncode}; inspect speech-worker.log')
            if select.select([self.child.stdout], [], [], .1)[0]:
                chunk = os.read(self.child.stdout.fileno(), 65536)
                if not chunk:
                    raise WorkerError('Speech worker closed its output')
                self.output_buffer += chunk
                if len(self.output_buffer) > 1024**2:
                    raise WorkerError('Speech worker response exceeds 1 MiB')
        raise WorkerError('Speech worker timed out')

    def ensure_child(self, expected_preparation=None):
        with self.lock:
            if expected_preparation is not None and (expected_preparation is not self.preparation
                    or expected_preparation['status'] != 'queued'):
                return
            active = getattr(self, 'active', None)
            if self.closed or (active is not None and self.jobs[active]['status'] in TERMINAL):
                raise WorkerError('Speech preparation cancelled')
            self.refresh_runtime()
            if self.runtime is not None and self.child and self.child.poll() is None:
                return self.runtime
            if not self.preparation or self.preparation['status'] != 'queued':
                self.preparation = self.new_preparation()
            self.prepare_requested = False
            operation = self.preparation
            operation.update(status='preparing', started_at=time.time())
            self.runtime_epoch += 1
            epoch = self.runtime_epoch
            self.runtime_state, self.runtime_error = 'preparing', None
            previous = self.child
            self.child = None
        try:
            if previous:
                previous.stdin.close()
                previous.stdout.close()
            remaining = self.prepare_deadline-time.monotonic()
            if remaining <= 0:
                raise WorkerError('Speech preparation timed out before startup')
            child = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.worker_log, bufsize=0)
            with self.lock:
                if self.closed or epoch != self.runtime_epoch:
                    self.kill(child)
                    child.stdout.close()
                    raise WorkerError('Speech preparation cancelled')
                self.child = child
                os.set_blocking(child.stdin.fileno(), False)
                self.output_buffer = b''
            ready = self.read_child(timeout=self.prepare_deadline-time.monotonic())
            if ready.get('ready') is not True:
                raise WorkerError(str(ready.get('error') or 'Speech worker did not become ready'))
            with self.lock:
                if self.closed or epoch != self.runtime_epoch:
                    raise WorkerError('Speech preparation cancelled')
                self.runtime = ready
                self.last_runtime = dict(ready)
                self.runtime_state, self.runtime_error = 'ready', None
                self.warmup_error = None
                operation.update(status='ready', finished_at=time.time())
                return ready
        except (OSError, WorkerError) as error:
            with self.lock:
                if epoch == self.runtime_epoch:
                    self.invalidate_runtime(str(error), 'failed')
                    self.warmup_error = str(error)
                self.kill(self.child)
                self.lock.notify_all()
            raise

    def write_child(self, job, request, deadline):
        pending = memoryview(json.dumps(request).encode()+b'\n')
        with self.lock:
            child, epoch = self.child, self.runtime_epoch
        while pending:
            with self.lock:
                if self.closed or job['status'] in TERMINAL:
                    return False
                if child is not self.child or epoch != self.runtime_epoch:
                    raise WorkerError('Speech runtime changed before the request completed')
                if time.monotonic() >= deadline:
                    raise WorkerError('Speech worker request pipe timed out')
                descriptor = child.stdin.fileno()
                try:
                    count = os.write(descriptor, pending)
                    pending = pending[count:]
                except BlockingIOError:
                    pass
            if pending:
                select.select([], [descriptor], [], min(.1, max(0, deadline-time.monotonic())))
        return True

    def run(self):
        try:
            self.run_worker()
        except Exception as error:
            self.fatal_error(error, 'worker')
            traceback.print_exc()

    def run_worker(self):
        with (self.root/'speech-worker.log').open('a') as self.worker_log:
            while True:
                with self.lock:
                    if self.worker_error is not None:
                        return
                    self.refresh_runtime()
                    self.lock.wait_for(lambda: self.pending or self.prepare_requested or self.closed or self.worker_error, timeout=.1)
                    if self.closed or self.worker_error:
                        return
                    if not self.pending and not self.prepare_requested:
                        continue
                    preparation = self.preparation if self.prepare_requested else None
                if preparation is not None:
                    try:
                        self.ensure_child(expected_preparation=preparation)
                    except (OSError, WorkerError):
                        pass
                    continue
                with self.lock:
                    if self.closed or self.worker_error:
                        return
                    if not self.pending:
                        continue
                    job = self.jobs[self.pending.popleft()]
                    if job['status'] in TERMINAL:
                        continue
                    if self.expire_before_first_audio(job):
                        continue
                    # Keep a bounded FIFO of text awaiting capacity. It reserves
                    # no audio and never synthesizes until the existing budget
                    # allows it. The original 30-second deadline still applies.
                    while job.get('admission_pending') and not self.closed and not self.worker_error and job['status'] not in TERMINAL:
                        if self.expire_before_first_audio(job):
                            break
                        if self.audio_backlog_seconds()+job['estimated_duration_seconds'] <= self.live_audio_budget_seconds:
                            job['admission_pending'] = False
                            job['admitted_at'] = time.time()
                            job['admission_wait_seconds'] = job['admitted_at']-job['created_at']
                            break
                        self.lock.wait(timeout=.1)
                    if self.closed:
                        return
                    if job['status'] in TERMINAL:
                        continue
                    if self.worker_error:
                        return
                    self.active = job['id']
                    job['status'] = 'processing'
                    job['started_at'] = time.time()
                    job['queue_seconds'] = job['started_at']-job['created_at']
                output = self.root/'results'/f"{job['id']}.wav"
                temporary = output.with_suffix('.tmp')
                try:
                    runtime = self.ensure_child()
                    with self.lock:
                        if self.closed or job['status'] in TERMINAL:
                            continue
                        job['early_audio'] = runtime.get('audio_stream_protocol') == 1 and job['playback']
                    job['continuous_playback'] = job['early_audio'] or (
                        self.playback_tempo == 'flexible' and job['live'] and job['playback'])
                    job['text_part_results'] = []
                    if job['early_audio']:
                        job['text_chunk_count'] = len(job['chunk_texts'])
                        job['chunk_count'] = 0
                    for index, text in enumerate(job['chunk_texts']):
                        with self.lock:
                            # Backpressure before computing, rather than discarding
                            # a finished WAV. Include the active player's remainder.
                            while (job['playback'] and self.buffered_audio_seconds()+
                                   estimated_audio_seconds(text, job['language']) > self.playback_limit_seconds):
                                if job['status'] in TERMINAL or self.closed or self.worker_error or self.expire_before_first_audio(job):
                                    break
                                self.lock.wait(timeout=.1)
                            if job['status'] in TERMINAL or self.closed or self.worker_error or self.expire_before_first_audio(job):
                                break
                        path = self.root/'results'/f"{job['id']}-part-{index:03d}.wav"
                        partial = path.with_suffix('.tmp')
                        limit = min(12, max(4, estimated_audio_seconds(text, job['language'])*2.5+2))
                        # Download-only non-live jobs retain their existing contract.
                        if not job['live'] and not job['playback']:
                            limit = job['max_duration_seconds']
                        request = {**job, 'text': text, 'max_duration_seconds': limit,
                            'reference': str(self.root/'voices'/f"{job['voice_id']}.wav"),
                            'output': str(partial)}
                        try:
                            timeout = min(self.timeout, 20) if job['live'] and self.delivery_policy != 'coverage' else self.timeout
                            deadline = time.monotonic()+timeout
                            if not self.write_child(job, request, deadline):
                                break
                            blocks = 0
                            result = self.read_child(timeout=deadline-time.monotonic())
                            while result.get('type') == 'audio':
                                filename = f"{job['id']}-part-{index:03d}-block-{blocks:05d}.wav"
                                if not job['early_audio'] or result.get('block_index') != blocks or result.get('filename') != filename:
                                    raise WorkerError('Early audio block order or filename is invalid')
                                block_path = self.root/'results'/filename
                                with wave.open(str(block_path), 'rb') as source:
                                    rate = source.getframerate()
                                    duration = source.getnframes()/rate
                                    if (source.getnchannels(), source.getsampwidth(), rate) != (1, 2, result['sample_rate']):
                                        raise WorkerError('Early audio WAV format is invalid')
                                if abs(duration-result['duration_seconds']) > 1/rate:
                                    raise WorkerError('Early audio WAV duration is invalid')
                                if duration <= 0 or duration > limit:
                                    raise WorkerError('Early audio block duration exceeds its limit')
                                chunk = {**result, 'job_id': job['id'], 'index': len(job['chunks']),
                                    'text_chunk_index': index, 'text': text, 'streamed': True,
                                    'filename': filename, 'status': 'ready', 'audio_ready_at': time.time()}
                                if not self.queue_audio(job, chunk):
                                    break
                                blocks += 1
                                result = self.read_child(timeout=deadline-time.monotonic())
                            if result.get('rejected') is True and 'error' in result:
                                raise TaskRejected(result['error'])
                            if 'error' in result:
                                raise WorkerError(result['error'])
                            if job['early_audio']:
                                with self.lock:
                                    if job['status'] in TERMINAL or self.closed:
                                        break
                                    if result.get('type') != 'complete' or result.get('stream_blocks') != blocks or not blocks:
                                        raise WorkerError('Early audio completion does not match the received blocks')
                                    partial.replace(path)
                                    job['text_part_results'].append({**result, 'text_chunk_index': index})
                                    job['synthesis_seconds'] = sum(p['synthesis_seconds'] for p in job['text_part_results'])
                                    if index == 0:
                                        job['first_text_part_synthesis_seconds'] = result['synthesis_seconds']
                                        job['first_callback_seconds'] = result.get('first_callback_seconds')
                                    self.save_job(job)
                                continue
                            with self.lock:
                                if job['status'] in TERMINAL or self.closed:
                                    break
                                partial.replace(path)
                                chunk = {**result, 'job_id': job['id'], 'index': index,
                                    'text': text, 'filename': path.name, 'status': 'ready',
                                    'audio_ready_at': time.time()}
                                if index == 0:
                                    job['first_callback_seconds'] = result.get('first_callback_seconds')
                            if not self.queue_audio(job, chunk):
                                break
                        finally:
                            partial.unlink(missing_ok=True)
                    with self.lock:
                        if job['status'] in TERMINAL or self.closed:
                            continue
                    # Preserve one ordered downloadable WAV for the logical job.
                    with wave.open(str(temporary), 'wb') as combined:
                        settings = None
                        for chunk in job['chunks']:
                            with wave.open(str(self.root/'results'/chunk['filename']), 'rb') as source:
                                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                                if settings is None:
                                    settings = current
                                    combined.setnchannels(current[0])
                                    combined.setsampwidth(current[1])
                                    combined.setframerate(current[2])
                                if current != settings:
                                    raise WorkerError('Speech chunk WAV formats differ')
                                combined.writeframes(source.readframes(source.getnframes()))
                    with self.lock:
                        if job['status'] in TERMINAL or self.closed:
                            continue
                        temporary.replace(output)
                        job['generation_done'] = True
                        job['generation_finished_at'] = time.time()
                        if not job['playback'] or (not job['continuous_playback'] and job['played_chunks'] == job['chunk_count']):
                            self.complete(job)
                except TaskRejected as error:
                    with self.lock:
                        if job['status'] not in TERMINAL:
                            job.update(status='failed', error=str(error), rejected=True)
                        if getattr(self, 'active_play', None) == job['id']:
                            self.kill(self.player)
                        self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
                        self.lock.notify_all()
                except (WorkerError, wave.Error, EOFError, subprocess.TimeoutExpired) as error:
                    with self.lock:
                        if job['status'] not in TERMINAL:
                            self.invalidate_runtime(str(error), 'failed')
                        self.kill(self.child)
                        if job['status'] not in TERMINAL:
                            job.update(status='failed', error=str(error))
                            if getattr(self, 'active_play', None) == job['id']:
                                self.kill(self.player)
                            self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
                except (OSError, sqlite3.Error) as error:
                    self.fatal_error(error, 'worker')
                finally:
                    temporary.unlink(missing_ok=True)
                    with self.lock:
                        if job['status'] in TERMINAL:
                            job['finished_at'] = time.time()
                        self.save_job(job)
                        if sys.exc_info()[0] is None:
                            self.active = None
                        self.prune()
                        self.lock.notify_all()

    def queue_audio(self, job, chunk):
        with self.lock:
            while (job['playback'] and self.buffered_audio_seconds()+
                   chunk['duration_seconds'] > self.playback_limit_seconds):
                if job['status'] in TERMINAL or self.closed or self.worker_error or self.expire_before_first_audio(job):
                    return False
                self.lock.wait(timeout=.1)
            if job['status'] in TERMINAL or self.closed or self.worker_error or self.expire_before_first_audio(job):
                return False
            job['chunks'].append(chunk)
            job['generated_chunks'] += 1
            if chunk.get('streamed'):
                job['chunk_count'] = len(job['chunks'])
            job['duration_seconds'] = sum(p['duration_seconds'] for p in job['chunks'])
            if not chunk.get('streamed'):
                job['synthesis_seconds'] = sum(p.get('synthesis_seconds', 0) for p in job['chunks'])
            job['sample_rate'] = chunk['sample_rate']
            job.setdefault('audio_ready_at', chunk['audio_ready_at'])
            if job['playback']:
                self.play_pending.append(chunk)
                if 'playback_started_at' not in job:
                    job['status'] = 'ready'
            self.save_job(job)
            self.lock.notify_all()
            return True

    def expire_before_first_audio(self, job):
        self.sync_playback_start(job)
        if self.delivery_policy == 'coverage':
            return False
        first_output = 'playback_started_at' if job['playback'] else 'audio_ready_at'
        if (job['live'] and first_output not in job and
                time.time()-job['created_at'] > 30):
            stage = ('admission' if job.get('admission_pending') else
                     'playback' if job.get('audio_ready_at') else 'synthesis')
            job.update(status='expired', finished_at=time.time(), expired_stage=stage,
                       error=f'Live speech did not reach first output within 30 seconds ({stage})')
            self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
            self.save_job(job)
            self.lock.notify_all()
            return True
        return False

    def complete(self, job):
        job['status'] = 'completed'
        voice = self.voices[job['voice_id']]
        voice['status'] = 'preview_available'
        atomic_json(self.root/'voices'/f"{voice['id']}.json", voice)

    def play_stream(self, job, first, log):
        with self.lock:
            if self.closed or job['status'] in TERMINAL:
                return
            self.active_play = job['id']
        try:
            with self.lock:
                if self.closed or job['status'] in TERMINAL:
                    return
                tempo = self.select_playback_tempo(job)
                job['playback_tempo'] = tempo
                job['playback_input_started_at'] = time.time()
                if tempo > 1:
                    capture = self.root/'results'/f"{job['id']}-playback.wav" if self.capture_playback else None
                    self.player = TempoPlayer(first['sample_rate'], self.device, tempo, log, capture)
                    job['playback_tempo_backend'] = self.player.backend
                else:
                    self.player = subprocess.Popen(['aplay', '-q', '-D', self.device,
                        '-t', 'raw', '-f', 'S16_LE', '-r', str(first['sample_rate']), '-c', '1'],
                        stdin=subprocess.PIPE, stderr=log, bufsize=0)
                self.player_job_id = job['id']
                os.set_blocking(self.player.stdin.fileno(), False)
            chunk = first
            while True:
                with self.lock:
                    if self.closed or job['status'] in TERMINAL:
                        return
                    self.active_play_piece = chunk
                    chunk.update(status='playing', playback_started_at=time.time(),
                        playback_tempo=tempo,
                        playback_timing=('tempo_filter_input' if tempo > 1 else
                            'null_paced' if self.device == 'null' else 'pipe_delivery'))
                    job['status'] = 'playing'
                    if tempo == 1:
                        job.setdefault('playback_started_at', chunk['playback_started_at'])
                    self.save_job(job)
                with wave.open(str(self.root/'results'/chunk['filename']), 'rb') as source:
                    if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, first['sample_rate']):
                        raise RuntimeError('Early audio playback formats differ')
                    pcm = memoryview(source.readframes(source.getnframes()))
                deadline = time.monotonic()+chunk['duration_seconds']+10
                while pcm:
                    if self.closed or job['status'] in TERMINAL:
                        return
                    if self.player.poll() is not None:
                        raise RuntimeError('Early audio playback process exited before the final block')
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Early audio playback pipe timed out')
                    try:
                        writable = select.select([], [self.player.stdin], [], .1)[1]
                        if writable:
                            count = os.write(self.player.stdin.fileno(), pcm)
                            pcm = pcm[count:]
                    except BlockingIOError:
                        pass
                    except (OSError, ValueError):
                        # 取消或标记拒绝已经关闭播放管道。只在任务确实结束时收尾，
                        # 其他管道错误仍然让播放组件致命失败。
                        with self.lock:
                            if self.closed or job['status'] in TERMINAL:
                                return
                        raise
                if self.device == 'null':
                    remaining = chunk['duration_seconds']/tempo-(time.time()-chunk['playback_started_at'])
                    if remaining > 0:
                        with self.lock:
                            self.lock.wait_for(lambda: self.closed or job['status'] in TERMINAL, timeout=remaining)
                with self.lock:
                    if self.closed or job['status'] in TERMINAL:
                        return
                    chunk.update(status='completed', finished_at=time.time())
                    job['played_chunks'] += 1
                    job['played_duration_seconds'] += chunk['duration_seconds']
                    self.active_play_piece = None
                    # 下一块已就绪时，在其开始记录中一并保存完成状态。
                    # 避免两次串行磁盘提交插在连续音频块之间。
                    if not (self.play_pending and self.play_pending[0]['job_id'] == job['id']):
                        self.save_job(job)
                    self.lock.notify_all()
                    self.lock.wait_for(lambda: self.closed or job['status'] in TERMINAL or
                        self.play_pending or job['generation_done'])
                    if self.closed or job['status'] in TERMINAL:
                        return
                    if self.play_pending and self.play_pending[0]['job_id'] == job['id']:
                        chunk = self.play_pending.popleft()
                    elif job['generation_done']:
                        break
                    else:
                        raise RuntimeError('Early audio playback order changed before completion')
            self.player.stdin.close()
            code = self.player.wait(timeout=self.playback_limit_seconds+10)
            if code:
                raise RuntimeError(f'Early audio playback failed (aplay exit {code})')
            with self.lock:
                if not self.closed and job['status'] not in TERMINAL:
                    if tempo > 1:
                        if not self.player.output_samples:
                            raise RuntimeError('Tempo playback produced no audio')
                        job['playback_output_samples'] = self.player.output_samples
                        job['playback_duration_seconds'] = self.player.output_samples/first['sample_rate']
                        if self.capture_playback:
                            job['playback_audio_filename'] = f"{job['id']}-playback.wav"
                    self.complete(job)
        except (RuntimeError, wave.Error, subprocess.TimeoutExpired) as error:
            with self.lock:
                if job['status'] not in TERMINAL:
                    job.update(status='failed', error=str(error))
                    self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
                    if getattr(self, 'active', None) == job['id']:
                        self.kill(self.child)
        finally:
            self.kill(self.player)
            if self.player and self.player.stdin and not self.player.stdin.closed:
                self.player.stdin.close()
            with self.lock:
                if job['status'] in TERMINAL:
                    job['finished_at'] = time.time()
                self.save_job(job)
                self.active_play = self.active_play_piece = None
                self.player_job_id = None
                self.prune()
                self.lock.notify_all()

    def select_playback_tempo(self, job):
        if self.playback_tempo == 'flexible' and job.get('live'):
            if job.get('chunk_texts'):
                job['duration_prediction'] = self.duration_estimator.predict(
                    job['chunk_texts'], job['language'], job['voice_revision'], self.runtime)
            prediction = job.get('duration_prediction', {}).get('seconds', job.get('estimated_duration_seconds', .8))
            duration = job.get('duration_seconds', prediction) if job.get('generation_done') else max(job.get('duration_seconds', 0), prediction)
            backlog = self.audio_backlog_seconds(include_deferred=True)
            own = max(job.get('duration_seconds', 0), job.get('estimated_duration_seconds', 0))-job.get('played_duration_seconds', 0)
            now = time.time()
            decision = self.tempo_controller.select(max(.01, duration), job.get('source_duration_seconds'),
                max(0, now-job.get('source_emitted_at', job['created_at'])),
                max(0, backlog-own)+duration, job.get('voice_revision', ''), now)
            decision['duration_basis'] = 'measured' if job.get('generation_done') else job.get('duration_prediction', {}).get('method', 'heuristic')
            decision['predicted_unserved_seconds'] = max(0, duration/decision['tempo']-(job.get('source_duration_seconds') or duration))
            job['tempo_decision'] = decision
            return decision['tempo']
        if self.playback_tempo != 'adaptive' or not job.get('live'):
            return 1.
        duration = max(job.get('duration_seconds', 0), job.get('estimated_duration_seconds', 0))
        ahead = max(0, self.audio_backlog_seconds(include_deferred=True)-duration+job.get('played_duration_seconds', 0))
        delay = max(ahead, time.time()-job['created_at'])
        # 每个逻辑任务固定一个速度，避免在 240 ms 块边界重置过滤器。
        return 1.05 if delay >= 1 else 1.

    def play_loop(self):
        job = None
        try:
            with (self.root/'playback.log').open('a') as log:
                while True:
                    job = None
                    with self.lock:
                        if self.worker_error is not None:
                            return
                        self.lock.wait_for(lambda: self.play_pending or self.closed or self.worker_error)
                        if self.closed or self.worker_error:
                            return
                        chunk = self.play_pending.popleft()
                        job = self.jobs[chunk['job_id']]
                        if job['status'] in TERMINAL or self.expire_before_first_audio(job):
                            continue
                    if chunk.get('streamed') or job.get('continuous_playback'):
                        self.play_stream(job, chunk, log)
                        job = None
                        continue
                    with self.lock:
                        if self.closed or job['status'] in TERMINAL:
                            continue
                        self.active_play = job['id']
                        self.active_play_piece = chunk
                        chunk['status'] = 'playing'
                        chunk['playback_started_at'] = time.time()
                        job['status'] = 'playing'
                        job.setdefault('playback_started_at', chunk['playback_started_at'])
                        self.save_job(job)
                    try:
                        with self.lock:
                            if job['status'] in TERMINAL or self.closed:
                                continue
                            self.player = subprocess.Popen(['aplay', '-q', '-D', self.device,
                                str(self.root/'results'/chunk['filename'])], stderr=log)
                        code = self.player.wait(timeout=chunk['duration_seconds']+10)
                        if code:
                            raise RuntimeError(f'Playback failed (aplay exit {code})')
                        if self.device == 'null':
                            remaining = chunk['duration_seconds']-(time.time()-chunk['playback_started_at'])
                            if remaining > 0:
                                with self.lock:
                                    self.lock.wait_for(lambda: job['status'] in TERMINAL or self.closed, timeout=remaining)
                        with self.lock:
                            if job['status'] not in TERMINAL and not self.closed:
                                chunk.update(status='completed', finished_at=time.time())
                                job['played_chunks'] += 1
                                job['played_duration_seconds'] += chunk['duration_seconds']
                                if job['generation_done'] and job['played_chunks'] == job['chunk_count']:
                                    self.complete(job)
                    except (RuntimeError, wave.Error, subprocess.TimeoutExpired) as error:
                        self.kill(self.player)
                        with self.lock:
                            if job['status'] not in TERMINAL:
                                job.update(status='failed', error=str(error))
                                self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
                                if getattr(self, 'active', None) == job['id']:
                                    self.kill(self.child)
                    finally:
                        with self.lock:
                            if job['status'] in TERMINAL:
                                job['finished_at'] = time.time()
                            self.save_job(job)
                            self.active_play = None
                            self.active_play_piece = None
                            self.prune()
                            self.lock.notify_all()
        except Exception as error:
            self.fatal_error(error, 'playback', [job] if job else ())
            traceback.print_exc()

    def close(self):
        restore = []
        with self.lock:
            closing = not self.closed
            if closing:
                self.closed = True
                self.invalidate_runtime('Speech service closed', 'closed')
                try:
                    if self.delivery_policy == 'coverage':
                        keys = self.jobs.ids("pending=0 AND status NOT IN ('completed','failed','cancelled','expired')")
                        jobs = [self.jobs[key] for key in keys]
                    else:
                        jobs = list(self.jobs.values())
                    for job in jobs:
                        if job['status'] not in TERMINAL:
                            if self.delivery_policy == 'coverage' and 'playback_started_at' not in job:
                                restore.append(json.loads(json.dumps(job)))
                            try:
                                self.cancel(job['id'])
                            except (OSError, sqlite3.Error) as error:
                                self.note_storage_failure(error)
                except (OSError, sqlite3.Error) as error:
                    self.note_storage_failure(error)
                self.lock.notify_all()
        self.kill(self.child)
        self.kill(self.player)
        self.play_worker.join(timeout=5)
        self.worker.join(timeout=5)
        if not self.worker.is_alive() and not self.play_worker.is_alive():
            with self.lock:
                if closing and not self.storage_failed:
                    for job in restore:
                        try:
                            self.reset_waiting(job)
                        except (OSError, sqlite3.Error) as error:
                            self.note_storage_failure(error)
                if self.delivery_policy == 'coverage':
                    try:
                        self.jobs.close()
                    except sqlite3.Error as error:
                        self.note_storage_failure(error)
        if self.child:
            self.child.stdin.close()
            self.child.stdout.close()

    def route(self, method, path, body):
        parts = urlsplit(path).path.strip('/').split('/')
        with self.lock:
            if parts == ['api', 'speech', 'status'] and method == 'GET':
                return self.status()
            if parts == ['api', 'speech', 'prepare'] and method == 'POST':
                return self.prepare()
            if parts == ['api', 'voices']:
                if method == 'GET':
                    return {'voices': list(self.voices.values())}
                if method == 'POST':
                    return self.add_voice(body)
            if len(parts) == 3 and parts[:2] == ['api', 'voices'] and method == 'DELETE':
                return self.delete_voice(parts[2])
            if parts == ['api', 'speech', 'session']:
                if method == 'GET':
                    return dict(self.session)
                if method == 'POST':
                    candidate = {**self.session, **{k: body[k] for k in self.session if k in body}}
                    if not isinstance(candidate['enabled'], bool) or not isinstance(candidate['playback'], bool):
                        raise ApiError(400, 'Session enabled and playback must be booleans')
                    if candidate['language'] not in self.status()['languages']:
                        raise ApiError(422, 'Output language unsupported')
                    if candidate['enabled'] and candidate['voice_id'] not in self.voices:
                        raise ApiError(404, 'Select a reference voice first')
                    self.session = candidate
                    return dict(self.session)
            if parts == ['api', 'speech', 'events'] and method == 'POST':
                if not self.session['enabled']:
                    return {'skipped': 'speech session disabled'}
                return self.submit({**body, **self.session, 'live': True})
            if parts == ['api', 'speech', 'stop'] and method == 'POST':
                self.session['enabled'] = False
                self.tempo_controller.previous.clear()
                stop_errors = 0
                try:
                    keys = self.jobs.ids("status NOT IN ('completed','failed','cancelled','expired')") if self.delivery_policy == 'coverage' else list(self.jobs)
                except sqlite3.Error as error:
                    # 队列读失败时仍执行停止，原因写入状态。
                    self.note_storage_failure(error)
                    keys, stop_errors = [], 1
                for job_id in keys:
                    # 单个任务的存储故障不中断停止流程。
                    try:
                        self.cancel(job_id)
                    except (OSError, sqlite3.Error) as error:
                        stop_errors += 1
                        if not self.storage_failed:
                            self.note_storage_failure(error)
                    except ApiError as error:
                        if error.code != 507:
                            raise
                        stop_errors += 1
                self.invalidate_runtime('Speech stopped', 'stopped')
                self.kill(self.child)
                self.prune()
                self.lock.notify_all()
                return {'stopped': True, 'stop_errors': stop_errors}
            if parts == ['api', 'speech', 'jobs']:
                if method == 'GET':
                    return {'jobs': self.status()['jobs']}
                if method == 'POST':
                    return self.submit(body)
            if len(parts) >= 4 and parts[:3] == ['api', 'speech', 'jobs']:
                job = self.jobs.get(parts[3])
                if job is None:
                    raise ApiError(404, 'Speech job not found')
                if len(parts) == 4 and method == 'GET':
                    return dict(job)
                if len(parts) == 4 and method == 'DELETE':
                    return self.cancel(parts[3])
                if parts[4:] == ['result'] and method == 'GET':
                    if job['status'] != 'completed':
                        raise ApiError(409, 'Speech result is not complete')
                    return (self.root/'results'/f"{job['id']}.wav").read_bytes()
        raise ApiError(404, 'Speech route not found')


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 <= length <= 7*1024**2:
                    raise ApiError(413, 'Speech request exceeds 7 MiB')
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ApiError(400, 'Request must be a JSON object')
                result = service.route(self.command, self.path, body)
                binary = isinstance(result, bytes)
                payload = result if binary else json.dumps(result, ensure_ascii=False).encode()
                code = 200
            except ApiError as error:
                payload, code, binary = json.dumps({'error': error.message}).encode(), error.code, False
            except (ValueError, TypeError):
                payload, code, binary = b'{"error":"Invalid JSON request"}', 400, False
            except Exception as error:
                print(f'[speech] request failed: {error}', file=sys.stderr, flush=True)
                payload, code, binary = b'{"error":"Speech service internal error"}', 500, False
            self.send_response(code)
            self.send_header('Content-Type', 'audio/wav' if binary else 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
        do_GET = do_POST = do_DELETE = handle_request
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--engine', choices=['pocket', 'zipvoice'], default='pocket')
    parser.add_argument('--vocoder', default='')
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--provider', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--precision', choices=['int8', 'float32'], default='int8')
    parser.add_argument('--provider-config', type=Path)
    parser.add_argument('--port', type=int, default=18081)
    parser.add_argument('--data-dir', type=Path, default=Path.home()/'.local/share/mozart/speech')
    parser.add_argument('--playback-device', default='default')
    parser.add_argument('--playback-tempo', choices=['normal', 'adaptive', 'flexible'], default='flexible')
    parser.add_argument('--duration-profile', type=Path, default=DEFAULT_PROFILE)
    parser.add_argument('--playback-tempo-max', type=float, default=DEFAULT_TEMPO_MAX)
    parser.add_argument('--playback-target-delay', type=float, default=1.5)
    parser.add_argument('--playback-recovery-seconds', type=float, default=30.)
    parser.add_argument('--capture-playback', action='store_true', help='Save filtered playback WAVs for diagnostics')
    parser.add_argument('--preload',action='store_true',help='Initialize the worker before admitting live input')
    parser.add_argument('--early-audio', action='store_true', help='Use the independent early Pocket audio runtime')
    parser.add_argument('--audio-chunk-frames', type=int, choices=[3, 5, 10, 15], default=3)
    parser.add_argument('--delivery-policy', choices=['coverage', 'realtime'], default='coverage')
    args = parser.parse_args()
    command = [sys.executable, str(Path(__file__).with_name('clone_worker.py')),
        '--model', args.model, '--engine', args.engine, '--threads', str(args.threads), '--vocoder', args.vocoder,
        '--provider', args.provider, '--precision', args.precision]
    if args.provider_config:
        command += ['--provider-config', str(args.provider_config)]
    if args.early_audio:
        command += ['--early-audio', '--audio-chunk-frames', str(args.audio_chunk_frames)]
    service = SpeechService(args.data_dir, command, args.engine, args.playback_device,
                            preload=args.preload, delivery_policy=args.delivery_policy,
                            playback_tempo=args.playback_tempo, capture_playback=args.capture_playback,
                            duration_profile=args.duration_profile, playback_tempo_max=args.playback_tempo_max,
                            playback_target_delay=args.playback_target_delay, playback_recovery_seconds=args.playback_recovery_seconds)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(service))
    server.daemon_threads = True
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    print(f'[speech] listening on 127.0.0.1:{args.port} engine={args.engine}', flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
